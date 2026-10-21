# -*- coding: utf-8 -*-
"""
HUD = video boyunca ekranda KALAN tasarim katmani (ASS overlay olarak uretilir).

Neden ASS: render zincirinde libass (subtitles filtresi) zaten var. drawtext ile
donmus yazi (dikey etiket) ve onlarca kutu cizmek mumkun degil/pahali; ASS bunlarin
hepsini (donme \\frz, cizim \\p1, fade \\fad, hassas konum \\pos) bedavaya veriyor.

Uretilen katmanlar (referans 768x1344; hepsi orana gore olceklenir):
  1) sol ust  : turuncu cizgi + KICKER (videonun degismeyen kimlik etiketi)
  2) sag ust  : META etiketi (konu kisa adi / toplam sure)
  3) sag kenar: dikey YAN ETIKET + ince cizgi
  4) sol orta : BOLUM BASLIGI (sections verilirse; fade ile girip cikar)
  5) alt      : bolum bolum dolan ILERLEME CUBUGU (sahne sayisi kadar segment)

Hicbiri zorunlu degil; alan yoksa o katman cizilmez, hepsi yoksa dosya uretilmez
(render eski haliyle calisir).

DIKKAT: ASS override kodlari ters boluyle baslar. Bu dosyayi duzenlerken normal
(raw olmayan) string icinde "\\\\an5" yazilmali; tek ters bolu ("\\a") Python'da
BEL karakteri olur ve etiket sessizce kaybolur.
"""

ORANGE = "&H0000A5FF&"     # ASS &HBBGGRR& -> RGB(255,165,0)
WHITE = "&H00FFFFFF&"


def _t(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600); sec -= h * 3600
    m = int(sec // 60); sec -= m * 60
    return f"{h:d}:{m:02d}:{sec:05.2f}"


def _tr_upper(s):
    """Turkce dogru buyuk harf (Python .upper() i->I yapiyor, yanlis)."""
    return str(s).replace("i", "İ").replace("ı", "I").upper()


def _esc(s):
    """ASS metninde anlam tasiyan karakterleri etkisizlestir."""
    return (str(s).replace("\\", "").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


def _rect(x, y, w, h, color, alpha, t0, t1, layer=1):
    """ASS cizim komutuyla (\\p1) dolu dikdortgen. Koordinatlar \\pos'a goredir."""
    return (f"Dialogue: {layer},{_t(t0)},{_t(t1)},HudDraw,,0,0,0,,"
            f"{{\\an7\\pos({x:.0f},{y:.0f})\\bord0\\shad0\\1c{color}\\1a&H{alpha:02X}&\\p1}}"
            f"m 0 0 l {w:.0f} 0 l {w:.0f} {h:.0f} l 0 {h:.0f}{{\\p0}}")


def build_hud_ass(out_path, width, height, total, hud, seg_durations=None, start_at=0.0):
    """HUD ASS dosyasini yazar. Cizilecek hicbir sey yoksa None doner."""
    hud = hud or {}
    kicker = _esc(hud.get("kicker") or "")
    side_label = _esc(hud.get("side_label") or "")
    meta_label = _esc(hud.get("meta_label") or "")
    sections = hud.get("sections") or []
    if not (kicker or side_label or meta_label or sections or seg_durations):
        return None

    # Referans 768x1344'e gore olcek (baska cozunurlukte de ayni gorunur).
    k = height / 1344.0
    M = round(48 * k)                     # kenar boslugu
    top_y = round(78 * k)                 # ust satir
    s_small = max(14, round(24 * k))      # kucuk etiket puntosu
    s_side = max(13, round(22 * k))       # dikey etiket puntosu
    s_title = max(24, round(46 * k))      # bolum basligi puntosu
    end = max(total, start_at + 0.1)

    styles = (
        f"Style: HudSmall,Arial,{s_small},{WHITE},{WHITE},&H00000000&,&H00000000&,"
        f"-1,0,0,0,100,100,3,0,1,2,0,7,0,0,0,1\n"
        f"Style: HudSide,Arial,{s_side},{WHITE},{WHITE},&H00000000&,&H00000000&,"
        f"-1,0,0,0,100,100,4,0,1,2,0,5,0,0,0,1\n"
        f"Style: HudTitle,Arial Black,{s_title},{WHITE},{WHITE},&H00000000&,&H00000000&,"
        f"-1,0,0,0,100,100,0,0,1,3,1,7,0,0,0,1\n"
        f"Style: HudDraw,Arial,20,{WHITE},{WHITE},&H00000000&,&H00000000&,"
        f"0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
    )

    ev = []

    # --- 1) sol ust: turuncu cizgi + kicker ---
    if kicker:
        dash_w, dash_h = round(34 * k), max(2, round(4 * k))
        ev.append(_rect(M, top_y + round(s_small * 0.45), dash_w, dash_h,
                        ORANGE, 0x00, start_at, end))
        ev.append(f"Dialogue: 1,{_t(start_at)},{_t(end)},HudSmall,,0,0,0,,"
                  f"{{\\an7\\pos({M + dash_w + round(14 * k)},{top_y})\\fad(400,0)}}"
                  f"{_tr_upper(kicker)}")

    # --- 2) sag ust: meta (konu kisa adi / toplam sure) ---
    if meta_label:
        meta = f"{_tr_upper(meta_label)} / {int(round(total))}s"
        ev.append(f"Dialogue: 1,{_t(start_at)},{_t(end)},HudSmall,,0,0,0,,"
                  f"{{\\an9\\pos({width - M},{top_y})\\1a&H50&\\fad(400,0)}}{meta}")

    # --- 3) sag kenar: dikey etiket + ince cizgi ---
    if side_label:
        cy = round(height * 0.34)
        # \frz-90: yazi yukaridan asagiya okunur (saat yonunde 90 derece donme).
        ev.append(f"Dialogue: 1,{_t(start_at)},{_t(end)},HudSide,,0,0,0,,"
                  f"{{\\an5\\pos({width - round(34 * k)},{cy})\\frz-90\\1a&H55&"
                  f"\\fad(500,0)}}{_tr_upper(side_label)}")
        line_h = round(len(side_label) * s_side * 0.62)
        ev.append(_rect(width - round(56 * k), cy - line_h // 2, max(1, round(2 * k)),
                        line_h, WHITE, 0x9A, start_at, end))

    # --- 4) sol orta: bolum basligi ---
    for sec in sections:
        try:
            t0 = max(float(sec.get("start", 0)), start_at)
            t1 = float(sec.get("end", t0))
        except (TypeError, ValueError):
            continue
        title = _esc(sec.get("title") or "")
        if not title or t1 <= t0:
            continue
        ev.append(f"Dialogue: 1,{_t(t0)},{_t(t1)},HudTitle,,0,0,0,,"
                  f"{{\\an7\\pos({M},{round(height * 0.60)})\\fad(350,350)}}"
                  f"{_tr_upper(title)}")

    # --- 5) alt: bolum bolum dolan ilerleme cubugu ---
    if seg_durations:
        n = len(seg_durations)
        bar_y = height - round(30 * k)
        bar_h = max(2, round(5 * k))
        gap = round(5 * k)
        avail = width - 2 * M - gap * (n - 1)
        if n > 0 and avail > n:
            segw = avail / n
            # zemin: tum segmentler sonuk beyaz
            for i in range(n):
                ev.append(_rect(M + i * (segw + gap), bar_y, segw, bar_h,
                                WHITE, 0xC8, start_at, end, layer=0))
            # dolum: sahne basladigi anda segment TURUNCU yanar ve oyle kalir
            t = 0.0
            for i in range(n):
                fill_t = max(t, start_at)
                if fill_t < end:
                    ev.append(_rect(M + i * (segw + gap), bar_y, segw, bar_h,
                                    ORANGE, 0x00, fill_t, end, layer=1))
                t += float(seg_durations[i])

    if not ev:
        return None

    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
              f"WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
              "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
              "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              + styles +
              "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
              "MarginV, Effect, Text\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(ev))
        f.write("\n")
    return out_path
