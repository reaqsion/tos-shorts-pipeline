"""
ÜRETİCİ (producer): Gemini'nin (ya da brain.py'ın) urettigi senaryo JSON'unu alir,
uctan uca video uretir. visual_mode'a gore FOTO ya da VIDEO pipeline'ini secer.

Kullanim:
  python produce.py scenario.json          # dosyadan
  Gemini ciktisini scenario.json'a yapistir -> calistir -> video hazir.

Senaryo JSON alanlari (Gemini MASTER_PROMPT ciktisi):
  topic, hook_overlay, narration, queries[], visual_mode ("video"|"photo"),
  youtube_title, youtube_description   (son ikisi yukleme icin saklanir)
"""
import requests, os, glob, json, sys, re
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den

ASSETS = os.environ.get("SHORTS_ASSETS_DIR",
                        os.path.dirname(os.path.abspath(__file__)))
# Cikti klasoru env ile degistirilebilir.
OUT_DIR = os.environ.get("SHORTS_OUT_DIR", os.path.join(ASSETS, "output"))
SCENE_SECONDS = 2.6
HOOK_OVERLAY_SECONDS = 1.5
MUSIC = os.path.join(ASSETS, "assets", "music", "cinematic1.mp3")
# SES TEMPOSU: Edge-TTS varsayilan hizi "spiker" gibi acele ediyordu; belgesel
# tonu icin hafif yavaslatiliyor. Kapatmak: setx SHORTS_TTS_RATE "+0%"
TTS_RATE = os.environ.get("SHORTS_TTS_RATE", "-8%")
SECTION_MAX_SECONDS = 5.0     # bolum basligi ekranda en fazla bu kadar kalir
SHORTS_MAX_SECONDS = 58.0     # bunu asarsa yavaslatmadan vazgecilir (Shorts siniri 60)


def _wav_seconds(path):
    import wave, contextlib
    try:
        with contextlib.closing(wave.open(path, "rb")) as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


def slugify(s):
    s = (s or "video").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s).strip()
    return "_".join(s.split()[:5]) or "video"


MIN_SCENE_SECONDS = 1.4         # bundan kisa sahne ekranda "flas" gibi gorunuyor


def smooth_durations(durations, min_len=MIN_SCENE_SECONDS):
    """Cok kisa sahneleri en uzun komsudan zaman odunc alarak duzeltir.

    Whisper hizalamasi bazen iki beat sinirini ust uste getiriyor: olcum,
    bir sahnenin 0.4 sn (goz kirpmasi), komsusunun 12.2 sn (donuk kare) surdugunu
    gosterdi. Toplam sure korunur -> ses/altyazi senkronu bozulmaz."""
    d = list(durations)
    for i, v in enumerate(d):
        if v >= min_len:
            continue
        j = max(range(len(d)), key=lambda k: d[k] if k != i else -1)
        take = min(min_len - v, max(0.0, d[j] - min_len))
        d[i] += take
        d[j] -= take
    return [round(x, 3) for x in d]


def write_beat_report(path, scenario, beats, durations, sources):
    """Her beat icin: metin -> sorgu -> secilen gorselin basligi/kaynagi/alakasi.

    'ALAKA' sutunu 0 ise o beat'e sorgusunu HIC tutmayan bir gorsel gitmistir
    (jenerik dolgu). Rapor sonundaki ozet, videoyu izlemeden kalite olcmeyi saglar."""
    if not beats:
        return
    lines = [f"KONU : {scenario.get('topic','')}",
             f"CAPA : {scenario.get('anchor','(yok)')}",
             f"HOOK : {scenario.get('hook_overlay') or scenario.get('hook','')}",
             f"MOD  : {scenario.get('visual_mode','photo')}", ""]
    zayif, tekrar = 0, 0
    for i, b in enumerate(beats):
        src = sources[i] if i < len(sources) else None
        rel = (src or {}).get("relevance", 0)
        if rel <= 0:
            zayif += 1
        if (src or {}).get("reused"):
            tekrar += 1
        sure = f"{durations[i]:.1f}s" if durations and i < len(durations) else "?"
        isim = (src or {}).get("title", "") or (src or {}).get("source_url", "")
        isim = isim.replace("File:", "")[:70]
        bayrak = "  " if rel > 0 else "!!"  # !! = alakasiz/jenerik dolgu
        clip = (src or {}).get("clip_score")
        clip_str = f" CLIP={clip:.3f}" if clip is not None else ""
        lines += [
            f"{bayrak} [{i+1:2}] {sure:>6}  {(src or {}).get('kind','?'):8} "
            f"ALAKA={rel:<7}{clip_str:<12} {(src or {}).get('source','YOK')}"
            f"{'  (KOMSUDAN TEKRAR)' if (src or {}).get('reused') else ''}",
            f"      metin  : {b.get('text','')}",
            f"      sorgu  : {b.get('query','')}",
            f"      gorsel : {isim}",
            "",
        ]
    toplam = len(beats)
    farkli = len({(sources[i] or {}).get("source_url") for i in range(min(toplam, len(sources)))
                  if sources[i]})
    lines.append(f"OZET: {toplam - zayif}/{toplam} beat sorgusunu tutan gorsel aldi "
                 f"({zayif} jenerik/alakasiz, {tekrar} komsudan tekrar). "
                 f"Videoda {farkli} FARKLI gorsel var.")
    # CLIP ozeti: varsa CLIP skorlarini listele
    clip_scores = [(i, (sources[i] or {}).get("clip_score", 0))
                   for i in range(min(toplam, len(sources)))
                   if (sources[i] or {}).get("clip_score")]
    if clip_scores:
        avg = sum(s for _, s in clip_scores) / len(clip_scores)
        lines.append(f"CLIP : {len(clip_scores)}/{toplam} beat CLIP ile siralandi "
                     f"(ort={avg:.3f})")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  RAPOR: {path}  ({toplam - zayif}/{toplam} beat alakali, "
          f"{farkli} farkli gorsel)")
    # KALITE KAPISI icin sayilar: run.py bunlara bakip yukleyip yuklemeyecegine karar verir.
    return {
        "beat": toplam,
        "alakali": toplam - zayif,
        "alaka_orani": round((toplam - zayif) / toplam, 3) if toplam else 0.0,
        "farkli_gorsel": farkli,
        "farkli_orani": round(farkli / toplam, 3) if toplam else 0.0,
        "komsudan_tekrar": tekrar,
    }


def _tr_upper(s):
    return str(s).replace("i", "İ").replace("ı", "I").upper()


def build_hud(scenario, beats, beat_times, durations, duration):
    """HUD (kalici tasarim katmani) verisini hazirlar.

    Gemini yeni alanlari (kicker/side_label/sections) vermezse konudan/capadan
    guvenli varsayilanlar turetilir -> ESKI senaryo JSON'lari da calisir."""
    topic = (scenario.get("topic") or "").strip()
    anchor = (scenario.get("anchor") or "").strip()

    kicker = (scenario.get("kicker") or "").strip()
    if not kicker:
        kicker = " ".join(topic.split()[:3])          # yedek: konunun ilk 3 kelimesi

    meta = (scenario.get("meta_label") or "").strip()
    if not meta:
        meta = " ".join((anchor or topic).split()[:2])

    # Bolum basliklari: beat indeksinden (1-tabanli) zamana cevrilir.
    sections = []
    raw = scenario.get("sections") or []
    starts = None
    if beat_times and len(beat_times) == len(beats or []):
        starts = [bt["start"] for bt in beat_times]
    elif durations:
        acc, starts = 0.0, []
        for d in durations:
            starts.append(acc)
            acc += d
    if starts:
        pts = []
        for s in raw:
            try:
                bi = int(s.get("beat", 0)) - 1
            except (TypeError, ValueError):
                continue
            title = (s.get("title") or "").strip()
            if title and 0 <= bi < len(starts):
                pts.append((starts[bi], title))
        pts.sort()
        for i, (t0, title) in enumerate(pts):
            nxt = pts[i + 1][0] if i + 1 < len(pts) else duration
            sections.append({"start": t0,
                             "end": min(nxt, t0 + SECTION_MAX_SECONDS),
                             "title": title})

    return {
        "kicker": _tr_upper(kicker) if kicker else "",
        "side_label": _tr_upper(scenario.get("side_label") or ""),
        "meta_label": meta,
        "sections": sections,
    }


def produce(scenario, voice=None):
    # Konu filtresi: Gemini telifli diye reddettiyse burada dur.
    if scenario.get("rejected"):
        print(f"KONU REDDEDİLDİ: {scenario.get('reason')}")
        print(f"Öneri: {scenario.get('alternative')}")
        return None

    beats = scenario.get("beats") or []
    narration = scenario["narration"]
    hook = scenario.get("hook_overlay") or scenario.get("hook") or ""
    queries = scenario.get("queries") or []
    mode = scenario.get("visual_mode", "photo")
    topic = scenario.get("topic", "video")
    # Beat turu (specific/generic) + konu capasi: gorsel-anlatim uyumunun son katmani.
    kinds = scenario.get("kinds") or None
    anchor = (scenario.get("anchor") or "").strip() or None
    # CLIP icin gorsel tarifleri (ozel isimsiz, kisa, somut).
    visuals = scenario.get("visuals") or None
    slug = slugify(topic)
    video_dir = os.path.join(OUT_DIR, slug)
    os.makedirs(video_dir, exist_ok=True)
    out = os.path.join(video_dir, f"{slug}.mp4")
    beat_texts = [b["text"] for b in beats] if beats else None

    # 1) TTS -> audio.wav (kanal sesi opsiyonel)
    tts_body = {"text": narration}
    if voice:
        tts_body["voice"] = voice
    if TTS_RATE:
        tts_body["rate"] = TTS_RATE
    r = requests.post("http://localhost:5002/", json=tts_body, timeout=300)
    r.raise_for_status()
    wav_path = os.path.join(ASSETS, "audio.wav")
    open(wav_path, "wb").write(r.content)
    # 60 sn KORUMASI: yavaslatma uzun bir anlatimi Shorts sinirinin ustune
    # itebilir. Ustune ciktiysa normal tempoyla bir kez daha sentezle.
    if TTS_RATE.startswith("-") and _wav_seconds(wav_path) > SHORTS_MAX_SECONDS:
        print(f"  ses {_wav_seconds(wav_path):.1f}s > {SHORTS_MAX_SECONDS}s -> normal tempoda yeniden")
        tts_body.pop("rate", None)
        r = requests.post("http://localhost:5002/", json=tts_body, timeout=300)
        r.raise_for_status()
        open(wav_path, "wb").write(r.content)
    print(f"  ses ok ({_wav_seconds(wav_path):.1f}s)")

    # 2) altyazi -> subs.ass (forced alignment) + BEAT zaman araliklari
    sub_req = {
        "audio": os.path.join(ASSETS, "audio.wav"),
        "output": os.path.join(ASSETS, "subs.ass"), "text": narration,
    }
    if beat_texts:
        sub_req["beats"] = beat_texts
    r = requests.post("http://localhost:5060/generate_subtitles", json=sub_req, timeout=300)
    r.raise_for_status()
    sj = r.json()
    duration = sj["duration"]
    beat_times = sj.get("beat_times")

    # BEAT-SENKRON: her gorsel TAM o cumle soylenirken gosterilir.
    # beat_times'i, baslangic zamanlarina gore sahne surelerine cevir.
    # Boylece bosluklar onceki beat'e kalir, sonraki gorsel konusma baslarken degisir.
    durations = None
    if beats and beat_times and len(beat_times) == len(beats):
        starts = [bt["start"] for bt in beat_times]
        durations = []
        for i in range(len(starts)):
            if i + 1 < len(starts):
                dur = starts[i+1] - starts[i]
            else:
                dur = duration - starts[i]
            durations.append(round(max(0.4, dur), 3))
        durations = smooth_durations(durations)
        scene_count = len(beats)
        print(f"  altyazi ok, sure={duration:.1f}s, beat={scene_count} (senkron)")
    else:
        scene_count = max(1, round(duration / SCENE_SECONDS))
        print(f"  altyazi ok, sure={duration:.1f}s, sahne={scene_count} (esit bol)")

    # 3+4) gorsel + render (visual_mode'a gore)
    for pat in ("scene_*.png", "clip_*.mp4"):
        for p in glob.glob(os.path.join(ASSETS, pat)):
            os.remove(p)

    payload = {
        "audio": os.path.join(ASSETS, "audio.wav"),
        "subtitles": os.path.join(ASSETS, "subs.ass"),
        "output": out, "hook": hook, "hook_overlay_seconds": HOOK_OVERLAY_SECONDS,
    }
    if os.path.exists(MUSIC):
        payload["music"] = MUSIC
        payload["music_volume"] = 0.15
    payload["hud"] = build_hud(scenario, beats, beat_times, durations, duration)

    use_per_query = bool(durations)     # beat senkron aktifse gorseller 1:1 hizali

    if mode == "video":
        r = requests.post("http://localhost:5080/fetch_clips", json={
            "subject": topic, "scene_count": scene_count, "queries": queries,
            "per_query": use_per_query, "kinds": kinds, "anchor": anchor,
            "visuals": visuals,
        }, timeout=600)
        r.raise_for_status()
        pj = r.json()
        assets = pj["clips"]
        print(f"  {len(assets)} klip | kaynak={pj['source_breakdown']}")
        payload["clips"] = assets
        endpoint = "http://localhost:5050/render_reels_video"
        sources = pj.get("sources") or []
    else:
        r = requests.post("http://localhost:5080/fetch_scenes", json={
            "subject": topic, "scene_count": scene_count,
            "narration": narration, "queries": queries, "per_query": use_per_query,
            "kinds": kinds, "anchor": anchor, "visuals": visuals,
        }, timeout=600)
        r.raise_for_status()
        pj = r.json()
        assets = pj["images"]
        print(f"  {len(assets)} foto | kaynak={pj['source_breakdown']}")
        payload["images"] = assets
        endpoint = "http://localhost:5050/render_reels"
        sources = pj.get("sources") or []

    # Sure listesini gercek gorsel sayisiyla hizala (bir beat'e gorsel gelmediyse
    # per_query komsuyla doldurur; yine de guvenli tarafta uzunlugu esitle).
    if durations and len(assets) == len(durations):
        payload["durations"] = durations
    elif durations:
        # gorsel sayisi degistiyse sureyi orantili yeniden bol
        total = sum(durations)
        payload["durations"] = [round(total / len(assets), 3)] * len(assets)

    r = requests.post(endpoint, json=payload, timeout=600)
    if not r.ok:
        # HATA GORUNURLUGU: eskiden sadece "400 Client Error" firliyordu; sunucunun
        # soyledigi SEBEP (orn. "Gorsel bulunamadi: scene_3.png") kayboluyordu.
        print(f"  RENDER HATASI {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    rj = r.json()

    # BEAT RAPORU: hangi cumleye hangi gorsel gitti? Kaliteyi videoyu izlemeden
    # 10 saniyede denetlemek icin. relevance=0 olan satirlar sorunlu beat'lerdir.
    kalite = write_beat_report(os.path.splitext(out)[0] + "_beats.txt",
                               scenario, beats, durations, sources)

    # YouTube meta verisini videonun yanina kaydet (yukleme adimi icin hazir)
    meta = {
        "title": scenario.get("youtube_title", topic),
        "description": scenario.get("youtube_description", ""),
        "video": rj["output"], "thumbnail": rj.get("thumbnail"),
        "visual_mode": mode,
        # KALITE KAPISI: run.py yuklemeden once bu sayilara bakar.
        "kalite": kalite or {},
    }
    with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"  VIDEO: {rj['output']}")
    print(f"  THUMB: {rj.get('thumbnail')}")
    print(f"  META : {os.path.splitext(out)[0] + '.json'}")
    return rj["output"]


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "scenario.json"
    scenario = json.load(open(path, encoding="utf-8"))
    print(f"=== ÜRETİLİYOR: {scenario.get('topic', path)} ({scenario.get('visual_mode','photo')}) ===")
    produce(scenario)
