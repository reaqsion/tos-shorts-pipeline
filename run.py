"""
ORKESTRATÖR: bir kanal icin tam zincir -> konu -> senaryo -> video -> YouTube.
Kanal profilleri channels.json'da. Konu kuyrugu topics_<kanal>.txt'de.

Kullanim:
  python run.py kanal1                    # sirada konu varsa onu, yoksa uret; video + yukle
  python run.py kanal1 --topic "Stuxnet"  # belirli konu
  python run.py kanal1 --dry              # video yap, YUKLEME (test)
  python run.py kanal1 --fill 10          # nise gore 10 konu uret, kuyruga ekle
  python run.py kanal1 --batch 3          # sirayla 3 video uret+yukle
"""
import datetime
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import sys, os, json
import brain
import produce

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS = os.path.join(HERE, "channels.json")


def load_channels():
    return json.load(open(CHANNELS, encoding="utf-8"))


def _topics_path(channel):
    return os.path.join(HERE, f"topics_{channel}.txt")


def _read_queue(channel):
    p = _topics_path(channel)
    if not os.path.exists(p):
        return []
    return [l.strip() for l in open(p, encoding="utf-8") if l.strip()]


def _write_queue(channel, items):
    with open(_topics_path(channel), "w", encoding="utf-8") as f:
        f.write("\n".join(items) + ("\n" if items else ""))


def fill_topics(channel, profile, n):
    existing = _read_queue(channel)
    new = brain.generate_topics(profile["niche"], n)
    _write_queue(channel, existing + new)
    print(f"{len(new)} konu üretildi, kuyruğa eklendi (toplam {len(existing)+len(new)}):")
    for t in new:
        print("  -", t)


def next_topic(channel, profile):
    q = _read_queue(channel)
    if q:
        topic = q[0]
        _write_queue(channel, q[1:])
        return topic
    gen = brain.generate_topics(profile["niche"], 1)
    return gen[0] if gen else None


# KALITE KAPISI esikleri (env ile degistirilebilir).
# Otomatige baglaninca kimse videoyu izlemiyor; kotu video da iyi video da ayni
# kolaylikla yayina gidiyordu. Olculen ornekler: Halifax 12/12 alakali + 11 farkli
# (iyi), Monte Cristo 11 beat'e 6 farkli gorsel (kotu, ayni tablo 4 kez).
MIN_ALAKA = float(os.environ.get("SHORTS_MIN_ALAKA", "0.80"))    # sorgusunu tutan beat orani
MIN_FARKLI = float(os.environ.get("SHORTS_MIN_FARKLI", "0.60"))  # farkli gorsel / beat


def kalite_kapisi(meta_path):
    """Meta'daki kalite sayilarina bakar. Doner: (yuklenebilir_mi, sebep)."""
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception as e:
        return False, f"meta okunamadi ({e})"
    k = meta.get("kalite") or {}
    if not k.get("beat"):
        # Eski senaryo/beat'siz uretim: kapi uygulanamaz, engelleme.
        return True, "kalite verisi yok (kapi atlandi)"
    sebepler = []
    if k.get("alaka_orani", 0) < MIN_ALAKA:
        sebepler.append(f"alaka {k['alakali']}/{k['beat']} "
                        f"(%{k['alaka_orani']*100:.0f} < %{MIN_ALAKA*100:.0f})")
    if k.get("farkli_orani", 0) < MIN_FARKLI:
        sebepler.append(f"farkli gorsel {k['farkli_gorsel']}/{k['beat']} "
                        f"(%{k['farkli_orani']*100:.0f} < %{MIN_FARKLI*100:.0f})")
    if sebepler:
        return False, "; ".join(sebepler)
    return True, "gecti"


def _kuyruga_geri_koy(channel, topic):
    """Konuyu kuyruğun başına geri koy (geçici hata sonrası kaybolmasın)."""
    q = _read_queue(channel)
    if topic not in q:
        _write_queue(channel, [topic] + q)


def _reddedileni_kaydet(channel, topic, sebep, video):
    """Kapiya takilan videoyu kaydet ki sabah bakip karar verebilesin.
    Konu kuyruga GERI KONMAZ: ayni konu tekrar denense buyuk ihtimalle yine
    takilir (gorsel arsivi zayif demektir), sonsuz donguye girer."""
    kayit = os.path.join(HERE, "reddedilen_videolar.txt")
    with open(kayit, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M}\t{channel}\t{topic}\t"
                f"{sebep}\t{video}\n")
    print(f"  kayit: {kayit}")


def run_one(channel, topic=None, dry=False):
    chans = load_channels()
    if channel not in chans:
        raise SystemExit(f"kanal yok: {channel} (channels.json)")
    profile = chans[channel]
    topic = topic or next_topic(channel, profile)
    if not topic:
        print("konu bulunamadı"); return None
    print(f"\n[{channel}] KONU: {topic}")

    try:
        scenario = brain.generate_scenario(topic, style=profile.get("style"))
    except Exception as e:
        # KONU KAYBI KORUMASI: konu kuyruktan ÇEKİLDİKTEN sonra senaryo üretimi
        # patlarsa (ölçüldü: Gemini'nin dört modeli birden 503), konu sessizce
        # yok oluyordu. Geçici hatada konuyu kuyruğun BAŞINA geri koy.
        _kuyruga_geri_koy(channel, topic)
        print(f"  SENARYO ÜRETİLEMEDİ ({type(e).__name__}) -> konu kuyruğa geri kondu")
        raise
    if scenario.get("rejected"):
        print("  REDDEDİLDİ:", scenario.get("reason"), "-> öneri:", scenario.get("alternative"))
        return None
    print("  HOOK:", scenario.get("hook_overlay"), "| MOD:", scenario.get("visual_mode"))

    video = produce.produce(scenario, voice=profile.get("voice"))
    if not video:
        return None
    meta_path = os.path.splitext(video)[0] + ".json"

    if dry:
        print("  [dry] yükleme atlandı. meta:", meta_path)
        return video

    gecti, sebep = kalite_kapisi(meta_path)
    if not gecti:
        print(f"  KALİTE KAPISI: YÜKLENMEDİ -> {sebep}")
        _reddedileni_kaydet(channel, topic, sebep, video)
        return video

    import upload  # sadece gerektiginde (OAuth)
    upload.upload(meta_path, privacy=profile.get("privacy", "unlisted"), channel=channel)
    return video


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    channel = args[0]

    if "--fill" in args:
        n = int(args[args.index("--fill") + 1])
        fill_topics(channel, load_channels()[channel], n)
        sys.exit(0)

    dry = "--dry" in args
    topic = args[args.index("--topic") + 1] if "--topic" in args else None
    batch = int(args[args.index("--batch") + 1]) if "--batch" in args else 1

    for i in range(batch):
        if batch > 1:
            print(f"\n===== {i+1}/{batch} =====")
        run_one(channel, topic if batch == 1 else None, dry)
