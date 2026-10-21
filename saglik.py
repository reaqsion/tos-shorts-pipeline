# -*- coding: utf-8 -*-
"""
SAGLIK KONTROLU — uretime baslamadan once her dis bagimliligi GERCEKTEN dener.

Neden var: 2026-10-15'te sistem haftalardir calismiyordu ve kimse bilmiyordu.
Alti ayri ariza sessizce olusmustu (Edge-TTS sesi olmus, Windows Turkce dil ayari
ffmpeg ciktisini cozemiyor, CLIP yolu dosya sizdiriyor...). Hicbiri log'a "HATA"
yazmiyordu; sadece uretim duruyordu. Bu betik, "port acik mi" ile yetinmez,
her katmandan KUCUK BIR IS ister -- bugunku arizalarin alti da boyle yakalanirdi.

Kullanim:
  python saglik.py            # tam kontrol, sorun varsa cikis kodu 1
  python saglik.py --hizli    # ag gerektiren gorsel testini atla

daily.py uretime baslamadan once bunu cagirir.
"""
import json
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import os
import socket
import subprocess
import sys
import tempfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
HIZLI = "--hizli" in sys.argv

SERVISLER = {5002: "edge_tts (ses)", 5060: "subtitle (Whisper)",
             5080: "photo (gorsel)", 5050: "ffmpeg (render)"}
OPSIYONEL = {5004: "visual_check (CLIP)"}

sonuclar = []     # (durum, baslik, aciklama) ; durum: OK / UYARI / HATA


def yaz(durum, baslik, aciklama=""):
    sonuclar.append((durum, baslik, aciklama))
    isaret = {"OK": "  OK  ", "UYARI": "UYARI ", "HATA": " HATA "}[durum]
    print(f"[{isaret}] {baslik}" + (f" -> {aciklama}" if aciklama else ""))


def port_acik(p):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", p))
        return True
    except Exception:
        return False
    finally:
        s.close()


def kontrol_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
        if r.returncode == 0:
            yaz("OK", "ffmpeg", r.stdout.split("\n")[0][:60])
        else:
            yaz("HATA", "ffmpeg", "calisti ama hata dondu")
    except Exception as e:
        yaz("HATA", "ffmpeg", f"bulunamadi ({type(e).__name__})")


def kontrol_anahtarlar():
    for ad, zorunlu in (("GEMINI_API_KEY", True), ("PIXABAY_API_KEY", True),
                        ("PEXELS_API_KEY", False)):
        if os.environ.get(ad):
            yaz("OK", ad, "tanimli")
        else:
            yaz("HATA" if zorunlu else "UYARI", ad,
                "YOK" + ("" if zorunlu else " (opsiyonel; video modu tek kaynaga duser)"))


def kontrol_portlar():
    for p, ad in SERVISLER.items():
        yaz("OK" if port_acik(p) else "HATA", f"port {p} - {ad}",
            "" if port_acik(p) else "KAPALI (start_all.bat)")
    for p, ad in OPSIYONEL.items():
        yaz("OK" if port_acik(p) else "UYARI", f"port {p} - {ad}",
            "" if port_acik(p) else "kapali (CLIP siralamasi devre disi)")


def kontrol_ses():
    """GERCEK sentez ister. Bugunku 1 numarali ariza (olu ses) tam burada cikardi."""
    if not port_acik(5002):
        yaz("HATA", "ses sentezi", "servis kapali, denenemedi")
        return None
    try:
        r = requests.post("http://localhost:5002/",
                          json={"text": "Sistem kontrol testi.", "voice": "tr_male"},
                          timeout=180)
        if r.status_code == 200 and len(r.content) > 2000:
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with open(wav, "wb") as f:
                f.write(r.content)
            yaz("OK", "ses sentezi", f"{len(r.content)} bayt")
            return wav
        yaz("HATA", "ses sentezi", f"HTTP {r.status_code} / {r.text[:120]}")
    except Exception as e:
        yaz("HATA", "ses sentezi", f"{type(e).__name__}")
    return None


def kontrol_altyazi(wav):
    if not wav:
        yaz("UYARI", "altyazi hizalama", "ses uretilemedigi icin atlandi")
        return
    try:
        ass_yol = os.path.join(tempfile.gettempdir(), "_saglik.ass")
        r = requests.post("http://localhost:5060/generate_subtitles",
                          json={"audio": wav, "output": ass_yol,
                                "text": "Sistem kontrol testi.",
                                "beats": ["Sistem kontrol testi."]},
                          timeout=300)
        if r.status_code == 200 and r.json().get("duration"):
            yaz("OK", "altyazi hizalama", f"{r.json()['duration']:.1f}s")
        else:
            yaz("HATA", "altyazi hizalama", f"HTTP {r.status_code}")
    except Exception as e:
        yaz("HATA", "altyazi hizalama", f"{type(e).__name__}")
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def _uretim_suruyor():
    """Su an bir run.py/daily.py uretimi var mi? (tasklist ile kaba kontrol)"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/V"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
        return ("run.py" in r.stdout) or ("daily.py" in r.stdout)
    except Exception:
        return False


def kontrol_gorsel():
    """Tek sorgulu kucuk bir cekim. Bugunku 2 ve 3 numarali arizalar burada cikardi
    (dil ayari cokmesi -> zaman asimi; CLIP dosya sizintisi -> 500)."""
    if HIZLI:
        yaz("UYARI", "gorsel cekimi", "--hizli ile atlandi")
        return
    # CAKISMA KORUMASI: /fetch_scenes ise baslarken ASSETS_DIR'deki TUM scene_*.png
    # dosyalarini siliyor. Uretim surerken bu testi yapmak, devam eden videonun
    # karelerini siler (2026-10-15'te tam bu oldu: kahve videosu 400 ile dustu).
    # Servisler sabit dosya adlari paylastigi icin es zamanli is guvenli degil.
    if _uretim_suruyor():
        yaz("UYARI", "gorsel cekimi",
            "uretim surerken atlandi (sabit dosya adlari cakisir)")
        return
    if not port_acik(5080):
        yaz("HATA", "gorsel cekimi", "servis kapali, denenemedi")
        return
    try:
        r = requests.post("http://localhost:5080/fetch_scenes",
                          json={"subject": "Istanbul", "queries": ["Istanbul Hagia Sophia"],
                                "visuals": ["a large domed mosque"], "kinds": ["specific"],
                                "anchor": "Hagia Sophia", "per_query": True},
                          timeout=240)
        if r.status_code == 200 and any(r.json().get("images") or []):
            kaynak = r.json().get("source_breakdown", {})
            yaz("OK", "gorsel cekimi", f"kaynak={kaynak}")
        else:
            yaz("HATA", "gorsel cekimi", f"HTTP {r.status_code} / {r.text[:150]}")
    except Exception as e:
        yaz("HATA", "gorsel cekimi", f"{type(e).__name__} (zaman asimi = yavas/bozuk)")


def kontrol_beyin():
    """Gemini'den kucuk bir cevap ister; 503 firtinasini onceden gorur."""
    if not os.environ.get("GEMINI_API_KEY"):
        yaz("HATA", "LLM (Gemini)", "anahtar yok, denenemedi")
        return
    try:
        sys.path.insert(0, HERE)
        import brain
        metin = brain._gemini_any({"contents": [{"role": "user",
                                                 "parts": [{"text": "sadece OK yaz"}]}]},
                                  timeout=60, tries=2)
        yaz("OK", "LLM (Gemini)", f"{brain.GEMINI_MODEL} -> {metin.strip()[:20]}")
    except Exception as e:
        kod = getattr(getattr(e, "response", None), "status_code", None)
        yaz("HATA", "LLM (Gemini)", f"{type(e).__name__} {kod or ''} (tum modeller mesgul)")


def kontrol_yukleme():
    """Yayin katmani: bilgi amacli, uretimi engellemez."""
    gizli = os.path.join(HERE, "client_secret.json")
    if not os.path.exists(gizli):
        yaz("UYARI", "YouTube OAuth", "client_secret.json YOK -> yukleme yapilamaz")
        return
    tokenlar = [f for f in os.listdir(HERE) if f.startswith("token_") and f.endswith(".json")]
    yaz("OK" if tokenlar else "UYARI", "YouTube OAuth",
        f"{len(tokenlar)} kanal yetkili" if tokenlar else "client_secret var, kanal yetkisi yok")


def main():
    print("=" * 62)
    print(" SAGLIK KONTROLU")
    print("=" * 62)
    kontrol_ffmpeg()
    kontrol_anahtarlar()
    kontrol_portlar()
    wav = kontrol_ses()
    kontrol_altyazi(wav)
    kontrol_gorsel()
    kontrol_beyin()
    kontrol_yukleme()

    hatalar = [s for s in sonuclar if s[0] == "HATA"]
    uyarilar = [s for s in sonuclar if s[0] == "UYARI"]
    print("-" * 62)
    print(f"SONUC: {len(sonuclar) - len(hatalar) - len(uyarilar)} saglikli, "
          f"{len(uyarilar)} uyari, {len(hatalar)} HATA")
    if hatalar:
        print("\nURETIM BASLAMAMALI. Once sunlari duzelt:")
        for _, b, a in hatalar:
            print(f"  - {b}: {a}")
    with open(os.path.join(HERE, "saglik_son.json"), "w", encoding="utf-8") as f:
        json.dump([{"durum": d, "baslik": b, "aciklama": a} for d, b, a in sonuclar],
                  f, ensure_ascii=False, indent=2)
    return 1 if hatalar else 0


if __name__ == "__main__":
    sys.exit(main())
