from flask import Flask, request, jsonify
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import subprocess
import os
import json
import tempfile

try:
    import hud as hud_mod          # kalici tasarim katmani (HUD) ureticisi
except Exception:                  # hud.py yoksa render eski haliyle calisir
    hud_mod = None

app = Flask(__name__)


# ---- Giris hook overlay yardimcilari (drawtext) ----
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",     # Arial Bold
    r"C:\Windows\Fonts\segoeuib.ttf",    # Segoe UI Bold
    r"C:\Windows\Fonts\Arial.ttf",
]


def _resolve_font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def _tr_upper(s):
    """Turkce-dogru buyuk harf: i->İ, ı->I."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def _wrap_hook(text, max_chars=16):
    """Hook metnini kelime bazli en fazla 3 satira sar."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    if len(lines) > 3 and max_chars < 40:
        return _wrap_hook(text, max_chars + 4)
    return lines[:3]


def _ass_time_to_sec(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _sec_to_ass_time(sec):
    h = int(sec // 3600); sec -= h * 3600
    m = int(sec // 60); sec -= m * 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def _trim_ass_before(ass_path, cutoff, tmpfiles):
    """Hook overlay suresince (0..cutoff) karaoke altyaziyi gizle:
    cutoff'tan once biten Dialogue'lari at; cutoff'a sarkanlarin
    baslangicini cutoff'a cek. Diger satirlar aynen kalir."""
    try:
        with open(ass_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ass_path
    out = []
    for ln in lines:
        if ln.startswith("Dialogue:"):
            head, rest = ln.split(":", 1)
            parts = rest.split(",", 9)
            try:
                start = _ass_time_to_sec(parts[1])
                end = _ass_time_to_sec(parts[2])
            except Exception:
                out.append(ln)
                continue
            if end <= cutoff:
                continue
            if start < cutoff:
                parts[1] = _sec_to_ass_time(cutoff)
            out.append(head + ":" + ",".join(parts))
        else:
            out.append(ln)
    fd, tmp = tempfile.mkstemp(suffix=".ass")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(out)
    tmpfiles.append(tmp)
    return tmp


def _build_hook_drawtext(base_label, hook_text, hook_seconds, width, height, tmpfiles):
    """scene_0 uzerine binen buyuk hook metni. Arkadaki foto akmaya devam
    eder (bu sadece ustune drawtext bindirir, videoyu dondurmaz).
    Her satir ayri drawtext ile YATAY ORTALANIR (drawtext coklu satiri sola
    yaslar). t=hook_seconds-0.3'ten itibaren fade-out."""
    font = _resolve_font()
    if not font or not hook_text or not hook_text.strip():
        return None, base_label
    lines = _wrap_hook(_tr_upper(hook_text.strip()))
    maxlen = max((len(l) for l in lines), default=1)
    target_w = 0.85 * width
    # 0.60: kalin BUYUK harf karakter genisligi (0.52 az tahmin ediyordu ->
    # "ŞAPKALARINDA" gibi uzun kelimeler kenardan tasip kirpiliyordu).
    fontsize = int(max(46, min(112, target_w / (0.60 * maxlen))))
    lh = fontsize * 1.25
    total_h = len(lines) * lh
    start_y = (height - total_h) / 2.0
    fade_start = max(0.0, hook_seconds - 0.3)
    enable = f"between(t,0,{hook_seconds:.2f})"
    alpha = f"if(lt(t,{fade_start:.2f}),1,max(0,({hook_seconds:.2f}-t)/0.3))"
    font_esc = escape_ffmpeg_filter_path(font)
    chain = []
    cur = base_label
    for i, line in enumerate(lines):
        fd, tf = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(line)
        tmpfiles.append(tf)
        tf_esc = escape_ffmpeg_filter_path(tf)
        y = start_y + i * lh + (lh - fontsize) / 2.0
        out_label = f"hk{i}"
        chain.append(
            f"[{cur}]drawtext=fontfile='{font_esc}':textfile='{tf_esc}':"
            f"fontcolor=white:fontsize={fontsize}:borderw=8:bordercolor=black:"
            f"shadowx=4:shadowy=4:shadowcolor=black@0.6:"
            f"x='(w-text_w)/2':y={y:.1f}:"
            f"enable='{enable}':alpha='{alpha}'[{out_label}]"
        )
        cur = out_label
    return ";".join(chain), cur


def _build_hud_chain(base_label, data, width, height, per_scene, start_at, tmpfiles):
    """HUD (kalici tasarim katmani) ASS'ini uretip filtre zincirine ekler.

    Iki subtitles filtresi ust uste binebilir (libass): once altyazi, sonra HUD.
    hud verisi gelmediyse ilerleme cubugu yine de cizilir (sahne surelerinden).
    HUD kapatmak icin: istekte "hud": false  ya  da  SHORTS_HUD=0."""
    if hud_mod is None or os.environ.get("SHORTS_HUD", "1") == "0":
        return None, base_label
    hud_data = data.get("hud")
    if hud_data is False:
        return None, base_label
    if not isinstance(hud_data, dict):
        hud_data = {}
    total = sum(per_scene)
    fd, path = tempfile.mkstemp(suffix=".ass")
    os.close(fd)
    tmpfiles.append(path)
    try:
        made = hud_mod.build_hud_ass(path, width, height, total, hud_data,
                                     seg_durations=per_scene, start_at=start_at)
    except Exception:
        made = None
    if not made:
        return None, base_label
    out_label = "vhud"
    return (f"[{base_label}]subtitles='{escape_ffmpeg_filter_path(path)}'[{out_label}]",
            out_label)


def probe_duration(path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1000:])
    return float(json.loads(result.stdout)["format"]["duration"])


def probe_dimensions(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1000:])
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def escape_ffmpeg_filter_path(path):
    # ffmpeg filter graph parser needs forward slashes and an escaped
    # drive-letter colon on Windows, e.g. C:\foo\bar.ass -> C\:/foo/bar.ass
    p = path.replace("\\", "/")
    p = p.replace(":", "\\:", 1)
    return p

@app.route('/render', methods=['POST'])
def render():
    data = request.get_json()
    image_path = data.get('image')
    audio_path = data.get('audio')
    output_path = data.get('output')

    if not image_path or not audio_path or not output_path:
        return jsonify({"error": "image, audio ve output parametreleri gerekli"}), 400

    if not os.path.exists(image_path):
        return jsonify({"error": f"Gorsel bulunamadi: {image_path}"}), 400
    if not os.path.exists(audio_path):
        return jsonify({"error": f"Ses dosyasi bulunamadi: {audio_path}"}), 400

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if result.returncode != 0:
        return jsonify({"error": "FFmpeg hata verdi", "details": result.stderr[-2000:]}), 500

    return jsonify({"success": True, "output": output_path})


@app.route('/render_multi', methods=['POST'])
def render_multi():
    data = request.get_json()
    images = data.get('images')
    audio_path = data.get('audio')
    subtitles_path = data.get('subtitles')
    output_path = data.get('output')

    if not images or not audio_path or not output_path:
        return jsonify({"error": "images, audio ve output parametreleri gerekli"}), 400
    if not isinstance(images, list) or len(images) == 0:
        return jsonify({"error": "images bos olmayan bir liste olmali"}), 400

    for img in images:
        if not os.path.exists(img):
            return jsonify({"error": f"Gorsel bulunamadi: {img}"}), 400
    if not os.path.exists(audio_path):
        return jsonify({"error": f"Ses dosyasi bulunamadi: {audio_path}"}), 400
    if subtitles_path and not os.path.exists(subtitles_path):
        return jsonify({"error": f"Altyazi dosyasi bulunamadi: {subtitles_path}"}), 400

    try:
        audio_duration = probe_duration(audio_path)
        width, height = probe_dimensions(images[0])
    except Exception as e:
        return jsonify({"error": f"Medya bilgisi okunamadi: {e}"}), 500

    scene_duration = audio_duration / len(images)

    # Each image becomes its own "-loop 1 -t ... -framerate 25" input and the
    # streams are joined with the concat FILTER (not the concat demuxer).
    # The demuxer's duration-directive trick only ever runs the subtitles
    # filter once per image (at pts=0) and then duplicates that single
    # rendered frame to fill the display time, which freezes word-by-word
    # karaoke highlighting for the whole scene. Real per-image inputs give
    # every duplicated frame a correctly incrementing pts, so the subtitles
    # filter re-evaluates the active word on every frame.
    input_args = []
    for img in images:
        input_args += ["-loop", "1", "-framerate", "25", "-t", f"{scene_duration:.4f}", "-i", img]

    scale_chains = "".join(
        f"[{i}:v]scale={width}:{height},setsar=1[v{i}];" for i in range(len(images))
    )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(images)))
    filter_complex = f"{scale_chains}{concat_inputs}concat=n={len(images)}:v=1:a=0[vcat]"
    if subtitles_path:
        filter_complex += f";[vcat]subtitles='{escape_ffmpeg_filter_path(subtitles_path)}'[vout]"
        video_map = "[vout]"
    else:
        video_map = "[vcat]"

    # Cikti klasoru yoksa olustur (ornegin outputs/ alt klasoru).
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    audio_index = len(images)
    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", video_map, "-map", f"{audio_index}:a",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if result.returncode != 0:
        return jsonify({"error": "FFmpeg hata verdi", "details": result.stderr[-2000:]}), 500

    return jsonify({
        "success": True,
        "output": output_path,
        "scene_count": len(images),
        "scene_duration": scene_duration
    })


@app.route('/render_reels', methods=['POST'])
def render_reels():
    """
    Retention'li render: her sahneye yavaş Ken Burns hareketi (zoom/pan),
    opsiyonel arka plan muzigi (konusma altinda otomatik kisilir) + altyazi.
    Durağan slayt yerine canli bir short uretir.
    """
    data = request.get_json()
    images = data.get('images')
    audio_path = data.get('audio')
    subtitles_path = data.get('subtitles')
    music_path = data.get('music')          # opsiyonel arka plan muzigi
    music_volume = float(data.get('music_volume', 0.18))
    output_path = data.get('output')

    if not images or not audio_path or not output_path:
        return jsonify({"error": "images, audio ve output parametreleri gerekli"}), 400
    if not isinstance(images, list) or len(images) == 0:
        return jsonify({"error": "images bos olmayan bir liste olmali"}), 400
    for img in images:
        if not os.path.exists(img):
            return jsonify({"error": f"Gorsel bulunamadi: {img}"}), 400
    if not os.path.exists(audio_path):
        return jsonify({"error": f"Ses dosyasi bulunamadi: {audio_path}"}), 400
    if subtitles_path and not os.path.exists(subtitles_path):
        return jsonify({"error": f"Altyazi dosyasi bulunamadi: {subtitles_path}"}), 400
    if music_path and not os.path.exists(music_path):
        return jsonify({"error": f"Muzik dosyasi bulunamadi: {music_path}"}), 400

    try:
        audio_duration = probe_duration(audio_path)
        width, height = probe_dimensions(images[0])
    except Exception as e:
        return jsonify({"error": f"Medya bilgisi okunamadi: {e}"}), 500

    n = len(images)
    fps = 30          # Shorts standardi (video motoruyla ayni)
    # BEAT-SENKRON: durations verilirse her sahne kendi suresini alir (gorsel
    # tam o cumle soylenirken gosterilir). Yoksa esit bol (eski davranis).
    durations = data.get('durations')
    if durations and len(durations) == n:
        per_scene = [max(0.4, float(d)) for d in durations]
    else:
        per_scene = [audio_duration / n] * n
    frames_list = [max(2, round(d * fps)) for d in per_scene]
    scene_duration = per_scene[0]

    # Her sahne kendi girdisi (PNG = TEK kare; zoompan d ile hareket uretir).
    # DIKKAT: "-loop 1" KOYMA -> sonsuz girdi olur, zoompan ilk sahneyi
    # sonsuz uretir, concat asla ilerlemez (tum video ilk foto olur).
    input_args = []
    for img in images:
        input_args += ["-i", img]
    audio_index = n
    input_args += ["-i", audio_path]
    music_index = None
    if music_path:
        music_index = n + 1
        input_args += ["-stream_loop", "-1", "-i", music_path]

    # Ken Burns (JITTER'SIZ): zoompan durağan goruntude x/y'yi tam sayiya
    # yuvarladigi icin titrer. Asil cozum: zoompan ciktisini 2x ver, sonra
    # hedef boyuta KUCULT (supersampling) -> alt-piksel puruzsuzluk.
    # Ön-olcek 2x yeterli (4x cok agirdi, render timeout ediyordu).
    pre_w, pre_h = width * 2, height * 2      # 1536x2688
    ss_w, ss_h = width * 2, height * 2        # 1536x2688 (supersample cikti)
    # TEMPO: zoom araligini genislet (1.0->1.22, ~%22) ve hizi kare sayisina
    # gore SABITLE (her sahne suresi ne olursa olsun ayni belirgin hareket).
    # Ayrica hafif yatay kayma (drift) ekle -> "canli" his. zoompan x/y'yi
    # gecerli araliga otomatik kirptigi icin siyah kenar riski yok.
    zmax = 1.22
    drift = 90.0                              # pre-scale (2x) piksel; hafif
    scene_filters = []
    for i in range(n):
        frames = frames_list[i]                                       # sahneye ozel
        zstep = (zmax - 1.0) / max(1, frames)
        if i % 2 == 0:
            zexpr = f"min(zoom+{zstep:.6f},{zmax})"                    # zoom-in
            xexpr = f"iw/2-(iw/zoom/2)+(on/{frames})*{drift}"         # saga kay
        else:
            zexpr = f"if(eq(on,0),{zmax},max(zoom-{zstep:.6f},1.0))"   # zoom-out
            xexpr = f"iw/2-(iw/zoom/2)-(on/{frames})*{drift}"        # sola kay
        scene_filters.append(
            f"[{i}:v]scale={pre_w}:{pre_h}:flags=lanczos,"
            f"zoompan=z='{zexpr}':d={frames}:"
            f"x='{xexpr}':y='ih/2-(ih/zoom/2)':"
            f"s={ss_w}x{ss_h}:fps={fps},"
            f"scale={width}:{height}:flags=lanczos,setsar=1[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    fc = ";".join(scene_filters) + ";" + concat_inputs + f"concat=n={n}:v=1:a=0[vc]"

    # Giris hook overlay parametreleri
    tmpfiles = []
    hook_text = data.get('hook')
    hook_seconds = float(data.get('hook_overlay_seconds', 1.5))
    hook_active = bool(hook_text and hook_text.strip() and hook_seconds > 0)

    if subtitles_path:
        subs_for_filter = subtitles_path
        if hook_active:
            # hook suresince karaoke altyaziyi gizle (overlay ile cakismasin)
            subs_for_filter = _trim_ass_before(subtitles_path, hook_seconds, tmpfiles)
        fc += f";[vc]subtitles='{escape_ffmpeg_filter_path(subs_for_filter)}'[vout]"
        video_map = "[vout]"
    else:
        video_map = "[vc]"

    # KALICI TASARIM KATMANI (HUD): ust etiketler, dikey yan etiket, bolum
    # basligi, alttaki ilerleme cubugu. Hook'tan SONRA girer (giris temiz kalsin).
    hud_fc, hud_label = _build_hud_chain(
        video_map.strip("[]"), data, width, height, per_scene,
        hook_seconds if hook_active else 0.0, tmpfiles)
    if hud_fc:
        fc += ";" + hud_fc
        video_map = f"[{hud_label}]"

    # scene_0 uzerine hook metni bindir (arkadaki foto akmaya devam eder)
    if hook_active:
        base = video_map.strip("[]")
        hook_fc, hook_label = _build_hook_drawtext(
            base, hook_text, hook_seconds, width, height, tmpfiles)
        if hook_fc:
            fc += ";" + hook_fc
            video_map = f"[{hook_label}]"

    # Ses: sadece konusma, ya da konusma + kisik muzik (ducking benzeri sabit dusuk seviye)
    if music_index is not None:
        fc += (
            f";[{music_index}:a]volume={music_volume}[bg]"
            f";[{audio_index}:a]volume=1.0[vo]"
            f";[vo][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = f"{audio_index}:a"

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", fc,
        "-map", video_map, "-map", audio_map,
        "-c:v", "libx264",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-shortest",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    for tf in tmpfiles:
        try:
            os.remove(tf)
        except OSError:
            pass

    if result.returncode != 0:
        return jsonify({"error": "FFmpeg hata verdi", "details": result.stderr[-2500:]}), 500

    # THUMBNAIL: ayni pipeline'da t=0.8 karesini (hook overlay ustunde) 1080x1920
    # PNG olarak videonun yanina yaz. YouTube upload YOK, sadece dosya.
    thumb_path = os.path.splitext(output_path)[0] + "_thumb.png"
    thumb_cmd = [
        "ffmpeg", "-y", "-ss", "0.8", "-i", output_path,
        "-frames:v", "1",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        thumb_path,
    ]
    thumb_res = subprocess.run(thumb_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    thumb_ok = thumb_res.returncode == 0 and os.path.exists(thumb_path)

    return jsonify({
        "success": True,
        "output": output_path,
        "scene_count": n,
        "scene_duration": scene_duration,
        "motion": True,
        "music": bool(music_path),
        "hook_overlay": hook_active,
        "thumbnail": thumb_path if thumb_ok else None,
    })


@app.route('/render_reels_video', methods=['POST'])
def render_reels_video():
    """render_reels'in VIDEO-KLIP surumu: statik foto + Ken Burns yerine
    her sahneye GERCEK video klip (Pexels/Pixabay/Archive.org, telifsiz).
    Klip sahne suresine kirpilir/loop'lanir, 9:16'ya doldurulur; ustune
    ayni katmanlar biner: karaoke altyazi + giris hook overlay + muzik.
    """
    data = request.get_json()
    clips = data.get('clips') or data.get('images')   # video klip yollari
    audio_path = data.get('audio')
    subtitles_path = data.get('subtitles')
    music_path = data.get('music')
    music_volume = float(data.get('music_volume', 0.15))
    output_path = data.get('output')
    TW, TH, fps = 1080, 1920, 30

    if not clips or not audio_path or not output_path:
        return jsonify({"error": "clips, audio ve output gerekli"}), 400
    if not isinstance(clips, list) or len(clips) == 0:
        return jsonify({"error": "clips bos olmayan liste olmali"}), 400
    for c in clips:
        if not os.path.exists(c):
            return jsonify({"error": f"Klip bulunamadi: {c}"}), 400
    if not os.path.exists(audio_path):
        return jsonify({"error": f"Ses bulunamadi: {audio_path}"}), 400

    try:
        audio_duration = probe_duration(audio_path)
    except Exception as e:
        return jsonify({"error": f"Ses okunamadi: {e}"}), 500

    n = len(clips)
    # BEAT-SENKRON: durations verilirse her klip kendi suresinde gosterilir
    # (klip tam o cumle soylenirken). Yoksa esit bol (eski davranis).
    durations = data.get('durations')
    if durations and len(durations) == n:
        per_scene = [max(0.4, float(d)) for d in durations]
    else:
        per_scene = [audio_duration / n] * n
    scene_duration = per_scene[0]

    tmpfiles = []
    hook_text = data.get('hook')
    hook_seconds = float(data.get('hook_overlay_seconds', 1.5))
    hook_active = bool(hook_text and hook_text.strip() and hook_seconds > 0)

    # Her klip: sonsuz loop + sahne suresinde kes (kisa klip doldurur, uzun
    # klip kirpilir). Sonra 9:16'yi DOLDUR (kirp) + fps sabitle.
    input_args = []
    for i, c in enumerate(clips):
        input_args += ["-stream_loop", "-1", "-t", f"{per_scene[i]:.4f}", "-i", c]
    audio_index = n
    input_args += ["-i", audio_path]
    music_index = None
    if music_path and os.path.exists(music_path):
        music_index = n + 1
        input_args += ["-stream_loop", "-1", "-i", music_path]

    scene_filters = []
    for i in range(n):
        scene_filters.append(
            f"[{i}:v]scale={TW}:{TH}:force_original_aspect_ratio=increase,"
            f"crop={TW}:{TH},setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    fc = ";".join(scene_filters) + ";" + concat_inputs + f"concat=n={n}:v=1:a=0[vc]"

    if subtitles_path and os.path.exists(subtitles_path):
        subs_for_filter = subtitles_path
        if hook_active:
            subs_for_filter = _trim_ass_before(subtitles_path, hook_seconds, tmpfiles)
        fc += f";[vc]subtitles='{escape_ffmpeg_filter_path(subs_for_filter)}'[vout]"
        video_map = "[vout]"
    else:
        video_map = "[vc]"

    # KALICI TASARIM KATMANI (HUD) - foto pipeline'iyla ayni
    hud_fc, hud_label = _build_hud_chain(
        video_map.strip("[]"), data, TW, TH, per_scene,
        hook_seconds if hook_active else 0.0, tmpfiles)
    if hud_fc:
        fc += ";" + hud_fc
        video_map = f"[{hud_label}]"

    if hook_active:
        base = video_map.strip("[]")
        hook_fc, hook_label = _build_hook_drawtext(
            base, hook_text, hook_seconds, TW, TH, tmpfiles)
        if hook_fc:
            fc += ";" + hook_fc
            video_map = f"[{hook_label}]"

    if music_index is not None:
        fc += (
            f";[{music_index}:a]volume={music_volume}[bg]"
            f";[{audio_index}:a]volume=1.0[vo]"
            f";[vo][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = f"{audio_index}:a"

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", *input_args,
        "-filter_complex", fc,
        "-map", video_map, "-map", audio_map,
        "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", "-r", str(fps), "-shortest",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    for tf in tmpfiles:
        try:
            os.remove(tf)
        except OSError:
            pass

    if result.returncode != 0:
        return jsonify({"error": "FFmpeg hata verdi", "details": result.stderr[-2500:]}), 500

    thumb_path = os.path.splitext(output_path)[0] + "_thumb.png"
    subprocess.run(["ffmpeg", "-y", "-ss", "0.8", "-i", output_path, "-frames:v", "1",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                    thumb_path], capture_output=True, text=True, encoding="utf-8", errors="replace")

    return jsonify({
        "success": True, "output": output_path, "scene_count": n,
        "scene_duration": scene_duration, "video_clips": True,
        "hook_overlay": hook_active, "music": bool(music_path),
        "thumbnail": thumb_path if os.path.exists(thumb_path) else None,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)