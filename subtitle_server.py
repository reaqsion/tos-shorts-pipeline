from flask import Flask, request, jsonify
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import stable_whisper
import wave
import os
import re
import contextlib

app = Flask(__name__)

# stable-ts: bilinen metni sese HIZALAR (forced alignment). Boylece altyazi
# kelimeleri birebir kaynak metinden gelir -> yabanci isim/kelime hatasi yok
# (ornek: Whisper "Sigmund"u "Sikmun", "psikanaliz"i kaba bir seye cevirmez).
# text gelmezse eski davranis: transcribe (Whisper tahmini).
print("Loading stable-ts model (small, cpu, int8)...")
model = stable_whisper.load_faster_whisper("small", device="cpu", compute_type="int8")
print("Model ready.")

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FONT_SIZE = 64
ACTIVE_COLOR = "&H0000A5FF&"   # ASS &HBBGGRR& -> turuncu
IDLE_COLOR = "&H00FFFFFF&"     # beyaz
MAX_WORDS_PER_CHUNK = 5

# ALTYAZI STILI: "box"  = sakin (koyu tam-genislik serit, tek satir, TEK kelime
#                         turuncu + alti cizili)  <- varsayilan
#                "karaoke" = eski hal (kelime kelime yanip sonen)
# Degistirmek icin: setx SHORTS_CAPTION_STYLE karaoke  (servisi yeniden baslat)
CAPTION_STYLE = os.environ.get("SHORTS_CAPTION_STYLE", "box").lower()
BOX_FONT_SIZE = 46

# Vurgu kelimesi bunlardan SECILMEZ (dolgu/baglac/zamir; _normalize_tr edilmis hal).
STOPWORDS = {
    "ve", "ile", "ama", "fakat", "ancak", "cunku", "gibi", "kadar", "sonra", "once",
    "daha", "cok", "az", "her", "hic", "bir", "biri", "bu", "su", "de", "da", "ki",
    "icin", "diye", "yine", "hem", "ya", "veya", "tum", "butun", "kendi", "boyle",
    "soyle", "oyle", "yani", "iste", "bile", "ise", "artik", "hala", "hep", "asla",
    "onun", "bunun", "onu", "bunu", "ona", "buna", "onlar", "bunlar", "sadece",
    "olarak", "uzere", "gore", "kere", "defa", "zaman", "seyi", "seye",
}


def get_wav_duration(path):
    with contextlib.closing(wave.open(path, 'rb')) as wf:
        return wf.getnframes() / float(wf.getframerate())


def format_ass_time(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def tr_upper(s):
    # Turkce dogru buyuk harf: i -> İ (Python .upper() yanlislikla I yapar).
    return s.replace("i", "İ").upper()


def clean_word(word):
    return tr_upper(word.strip()).replace("{", "").replace("}", "")


def group_words(words, max_words=MAX_WORDS_PER_CHUNK):
    chunks, current = [], []
    for w in words:
        current.append(w)
        if len(current) >= max_words:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _strip_punct(w):
    return re.sub(r"[^\w]", "", str(w).strip(), flags=re.UNICODE)


def _ass_text(w):
    """ASS'te anlam tasiyan karakterleri etkisizlestir.
    ONEMLI: buyuk/kucuk harf DEGISTIRILMEZ - kutu stilinde cumle kendi
    yazimiyla gorunur (hepsi BUYUK harf 'bagirma' hissi veriyordu)."""
    return str(w).strip().replace("\\", "").replace("{", "(").replace("}", ")")


def group_words_box(words, max_words=4, max_chars=30):
    """Kutu stili icin gruplama: hem kelime hem KARAKTER siniri.
    Turkce kelimeler uzun; sadece kelime sayisina bakinca serit tasiyordu."""
    chunks, cur, cur_len = [], [], 0
    for w in words:
        t = _ass_text(w[0])
        if cur and (len(cur) >= max_words or cur_len + 1 + len(t) > max_chars):
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(w)
        cur_len += (1 if cur_len else 0) + len(t)
        # CUMLE SINIRINDA KES: bir satirda iki ayri cumlenin parcasi bulunmasin
        # ("...bozmadi. Ancak 1995" gibi) - okumayi zorlastiriyordu.
        if t.endswith((".", "!", "?", "…", ":")):
            chunks.append(cur)
            cur, cur_len = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def _pick_keyword_index(chunk):
    """Turuncu + alti cizili gosterilecek kelimeyi sec: dolgu olmayan EN UZUN
    kelime. Sabit kural oldugu icin ayni cumle her render'da ayni vurguyu alir."""
    best_i, best_len = -1, 0
    for i, w in enumerate(chunk):
        t = _strip_punct(w[0])
        if len(t) < 4 or _normalize_tr(t) in STOPWORDS:
            continue
        if len(t) > best_len:
            best_i, best_len = i, len(t)
    return best_i


def build_ass_box(words, output_path):
    """SAKIN STIL: koyu tam-genislik serit + tek satir + TEK kelime vurgusu.

    Karaokeden farki: goz her karede yeniden odaklanmiyor. Izleyici cumleyi
    OKUYOR, yanip sonen kelimeyi TAKIP etmiyor -> "yorucu/PowerPoint" hissi gider."""
    bar_h = round(VIDEO_HEIGHT * 0.0655)            # ~88 px @1344
    bar_cy = round(VIDEO_HEIGHT * 0.897)            # seridin dikey merkezi
    bar_y = bar_cy - bar_h // 2
    bar_m = round(VIDEO_WIDTH * 0.0664)             # ~51 px @768 kenar bosluğu
    bar_w = VIDEO_WIDTH - 2 * bar_m
    accent_w = max(3, round(VIDEO_WIDTH * 0.0052))  # sol kenardaki turuncu vurgu
    pad = round(VIDEO_WIDTH * 0.03)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{BOX_FONT_SIZE},{IDLE_COLOR},{IDLE_COLOR},&H00000000&,&H00000000&,-1,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1
Style: CapBox,Arial,20,{IDLE_COLOR},{IDLE_COLOR},&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = group_words_box(words)
    lines = []
    for ci, chunk in enumerate(chunks):
        start = chunk[0][1]
        # bir sonraki gruba kadar ekranda kal -> altyazi bosluk birakmaz (titremez)
        end = chunks[ci + 1][0][1] if ci + 1 < len(chunks) else chunk[-1][2] + 0.25
        if end <= start:
            end = start + 0.05
        t0, t1 = format_ass_time(start), format_ass_time(end)

        # katman 0: koyu serit + sol turuncu vurgu (ASS cizim komutu)
        lines.append(
            f"Dialogue: 0,{t0},{t1},CapBox,,0,0,0,,"
            f"{{\\pos({bar_m},{bar_y})\\an7\\bord0\\shad0\\1c&H000000&\\1a&H12&\\p1}}"
            f"m 0 0 l {bar_w} 0 l {bar_w} {bar_h} l 0 {bar_h}{{\\p0}}"
        )
        lines.append(
            f"Dialogue: 0,{t0},{t1},CapBox,,0,0,0,,"
            f"{{\\pos({bar_m},{bar_y})\\an7\\bord0\\shad0\\1c{ACTIVE_COLOR}\\1a&H00&\\p1}}"
            f"m 0 0 l {accent_w} 0 l {accent_w} {bar_h} l 0 {bar_h}{{\\p0}}"
        )

        # katman 1: metin. Uzun satirda punto kucultulur -> seritten TASMAZ.
        ki = _pick_keyword_index(chunk)
        plain = " ".join(_ass_text(w[0]) for w in chunk)
        fs = BOX_FONT_SIZE
        est = len(plain) * fs * 0.52
        if est > bar_w - 2 * pad and len(plain) > 0:
            fs = max(30, int((bar_w - 2 * pad) / (0.52 * len(plain))))
        parts = []
        for j, w in enumerate(chunk):
            t = _ass_text(w[0])
            if j == ki:
                # noktalama vurgunun DISINDA kalsin (alti cizili nokta cirkin duruyor)
                m = re.match(r"^(.*?)([\.,;:!\?…»\"']*)$", t, flags=re.UNICODE)
                core, tail = (m.group(1), m.group(2)) if m and m.group(1) else (t, "")
                parts.append("{\\c" + ACTIVE_COLOR + "\\u1}" + core
                             + "{\\u0\\c" + IDLE_COLOR + "}" + tail)
            else:
                parts.append(t)
        lines.append(
            f"Dialogue: 1,{t0},{t1},Caption,,0,0,0,,"
            f"{{\\pos({VIDEO_WIDTH // 2},{bar_cy})\\an5\\fs{fs}}}" + " ".join(parts)
        )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")


def build_ass_kinetic(words, output_path):
    """KINETIC TYPOGRAPHY: kelimeler tek tek, sese senkron, POP ederek girer.

    Kutu stilinden farki: satir hazir gelmiyor: her kelime kendi konusuldugu
    anda buyuyerek beliriyor (\\t ile olcek animasyonu), onceki kelimeler
    ekranda kaliyor. Aktif kelime turuncu, oncekiler beyaz.
    Arka planda serit YOK - hareketli B-roll uzerine dogrudan biniyor, o yuzden
    kalin siyah kontur + golge ile okunurluk saglaniyor."""
    fs = round(VIDEO_HEIGHT * 0.042)          # ~56 px @1344
    cy = round(VIDEO_HEIGHT * 0.80)           # alt-orta
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kin,Arial Black,{fs},{IDLE_COLOR},{IDLE_COLOR},&H00000000&,&H80000000&,-1,0,0,0,100,100,0,0,1,5,3,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for ci, chunk in enumerate(group_words_box(words, max_words=3, max_chars=22)):
        chunk_end = chunk[-1][2] + 0.12
        for i, active in enumerate(chunk):
            start = active[1]
            end = chunk_end
            if end <= start:
                end = start + 0.08
            parts = []
            for j, w in enumerate(chunk):
                if j > i:
                    break
                t = _ass_text(w[0])
                if j == i:
                    # POP: 130% -> 100% olcek, 110 ms icinde
                    parts.append("{\\c" + ACTIVE_COLOR + "\\fscx130\\fscy130"
                                 "\\t(0,110,\\fscx100\\fscy100)}" + t
                                 + "{\\c" + IDLE_COLOR + "}")
                else:
                    parts.append(t)
            lines.append(
                f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Kin,,0,0,0,,"
                f"{{\\an5\\pos({VIDEO_WIDTH // 2},{cy})}}" + " ".join(parts)
            )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")


def build_ass(words, output_path):
    """Stil secici: box (varsayilan) | kinetic | karaoke (eski)."""
    if CAPTION_STYLE == "karaoke":
        return build_ass_karaoke(words, output_path)
    if CAPTION_STYLE == "kinetic":
        return build_ass_kinetic(words, output_path)
    return build_ass_box(words, output_path)


def build_ass_karaoke(words, output_path):
    """ESKI STIL: 5 kelimelik grup, aktif kelime turuncu, hepsi BUYUK harf."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,{FONT_SIZE},{IDLE_COLOR},{IDLE_COLOR},&H00000000&,&H00000000&,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for chunk in group_words(words):
        for i, active in enumerate(chunk):
            start = active[1]
            end = chunk[i + 1][1] if i + 1 < len(chunk) else active[2]
            if end <= start:
                end = start + 0.05
            parts = []
            for j, w in enumerate(chunk):
                text = clean_word(w[0])
                if j == i:
                    parts.append("{\\c" + ACTIVE_COLOR + "}" + text + "{\\c" + IDLE_COLOR + "}")
                else:
                    parts.append(text)
            lines.append(
                f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Caption,,0,0,0,,{' '.join(parts)}"
            )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")


def extract_words(result):
    """stable-ts WhisperResult -> (text, start, end) listesi."""
    words = []
    for w in result.all_words():
        if w.start is None or w.end is None:
            continue
        words.append((w.word, float(w.start), float(w.end)))
    return words


def _normalize_tr(s):
    """Turkce kelimeleri karsilastirma icin normalize et."""
    import re as _re
    s = s.lower().strip()
    s = _re.sub(r"[^\w\s]", "", s)  # noktalama temizle
    s = s.replace("ı", "i").replace("İ", "i").replace("ğ", "g").replace("ü", "u")
    s = s.replace("ş", "s").replace("ö", "o").replace("ç", "c")
    return s


def beat_time_ranges(words, beats, duration):
    """Her beat'i, Whisper'in hizaladigi kelimeler esleserek zaman araligini bulur.

    Once her beat'in normalized metnindeki kelimeleri sirayla aligned
    word listesinde esler (sliding window). Boylece Türkçe ek/concat
    farklilikari cascade hataya yol acmaz.
    """
    if not words or not beats:
        return [{"start": 0, "end": duration}] * len(beats) if beats else []

    # Align edilmis kelimeleri normalize et
    norm_words = [(i, _normalize_tr(w[0]), w[1], w[2]) for i, w in enumerate(words)]

    ranges = []
    word_ptr = 0  # mevcut pozisyon (linear tarama)

    for beat_text in beats:
        beat_words_raw = str(beat_text).split()
        beat_words = [_normalize_tr(w) for w in beat_words_raw if len(_normalize_tr(w)) > 1]

        if not beat_words:
            # boss beat: onceki bitis + 0.1sn
            last_end = ranges[-1]["end"] if ranges else 0.0
            ranges.append({"start": last_end, "end": min(last_end + 0.1, duration)})
            continue

        # Mevcut pozisyondan itibaren esleme ara (linear scan)
        matched_indices = []
        search_from = word_ptr
        bi = 0  # beat kelime index

        while bi < len(beat_words) and search_from < len(norm_words):
            if norm_words[search_from][1] == beat_words[bi]:
                matched_indices.append(search_from)
                bi += 1
                search_from += 1
            else:
                search_from += 1
                # cok fazla atladiysa dur
                if search_from - (word_ptr if matched_indices else search_from) > 15:
                    break

        if matched_indices:
            first_idx = matched_indices[0]
            last_idx = matched_indices[-1]
            ranges.append({
                "start": norm_words[first_idx][2],
                "end": norm_words[last_idx][3]
            })
            word_ptr = last_idx + 1  # bir sonraki beat buradan bassin
        else:
            # esleme bulunamadi: onceki bitis ile sonraki baslangic arasi
            last_end = ranges[-1]["end"] if ranges else 0.0
            # bir sonraki aligned word'un baslangicini bulmaya calis
            next_start = norm_words[min(word_ptr, len(norm_words)-1)][2] if word_ptr < len(norm_words) else duration
            ranges.append({"start": last_end, "end": next_start})

    # Son beat'i sesin sonuna uzat
    if ranges:
        ranges[-1]["end"] = max(ranges[-1]["end"], duration)

    # Monotoniklik sagla (bitisik araliklarda cakisma/kesme yok)
    for i in range(1, len(ranges)):
        if ranges[i]["start"] < ranges[i-1]["end"]:
            ranges[i]["start"] = ranges[i-1]["end"]
        if ranges[i]["end"] < ranges[i]["start"]:
            ranges[i]["end"] = ranges[i]["start"]

    return ranges


@app.route('/generate_subtitles', methods=['POST'])
def generate_subtitles():
    data = request.get_json(silent=True) or {}
    audio_path = data.get('audio')
    output_path = data.get('output')
    text = data.get('text')                 # varsa: forced alignment
    beats = data.get('beats')               # varsa: her beat'in zaman araligi doner
    language = data.get('language', 'tr')

    if not audio_path:
        return jsonify({"error": "audio parametresi gerekli"}), 400
    if not os.path.exists(audio_path):
        return jsonify({"error": f"Ses dosyasi bulunamadi: {audio_path}"}), 400
    if not output_path:
        output_path = os.path.splitext(audio_path)[0] + ".ass"

    try:
        duration = get_wav_duration(audio_path)
    except Exception as e:
        return jsonify({"error": f"Ses suresi okunamadi: {e}"}), 500

    try:
        if text and text.strip():
            # DOGRU YOL: bilinen metni hizala -> kelimeler birebir dogru
            result = model.align(audio_path, text, language=language)
            mode = "align"
        else:
            # Yedek: transcribe (Whisper tahmini)
            result = model.transcribe(audio_path, language=language, word_timestamps=True)
            mode = "transcribe"
    except Exception as e:
        return jsonify({"error": f"Altyazi olusturma hatasi ({e})"}), 500

    words = extract_words(result)
    if not words:
        return jsonify({"error": "Kelime zaman damgasi cikarilamadi"}), 500

    try:
        build_ass(words, output_path)
    except Exception as e:
        return jsonify({"error": f"ASS dosyasi olusturulamadi: {e}"}), 500

    resp = {
        "success": True,
        "ass_path": output_path,
        "duration": duration,
        "word_count": len(words),
        "mode": mode,
    }
    if beats:
        try:
            resp["beat_times"] = beat_time_ranges(words, beats, duration)
        except Exception as e:
            resp["beat_times_error"] = str(e)
    return jsonify(resp)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5060)
