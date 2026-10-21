from flask import Flask, request, jsonify
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import requests
import subprocess
import os
import glob
import tempfile
import re

app = Flask(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
    # Wikimedia API aciklayici User-Agent ister, yoksa bloklayabilir.
    "User-Agent": "ShortsAutomation/1.0 (educational history content; "
                  + os.environ.get("CONTACT_EMAIL", "contact-not-set") + ")"
}
# Proje klasoru tasinabilir olsun: varsayilan = bu betigin bulundugu klasor.
ASSETS_DIR = os.environ.get("SHORTS_ASSETS_DIR",
                            os.path.dirname(os.path.abspath(__file__)))
W, H = 1080, 1920

# HOOK/KALITE puanlama: hard-eleme degil, YUMUSAK ceza. Iyi bir portre
# (tek kisi, yuz, dikey) hook karesi icin idealdir; grup/tesrifat/uniforma
# fotolari (birden fazla kisi, yatay, baglamsal) geri duser.
SOFT_PENALTY = [
    "with", "and", "navy", "hms", "admiral", "uniform", "military",
    "premiere", "premier", "carpet", "festival", "award", "ceremony",
    "panel", "conference", "press", "interview", "audience", "crowd",
    "group", "team", "fans", "signing", "meeting", "summit", "visit",
    "wax", "madame tussauds", "figure", "sketch", "drawing", "painting",
]
PORTRAIT_BONUS_TERMS = ["portrait", "headshot", "closeup", "close-up", "face"]


# Kisi aramalarinda alakasiz sonuclari (heykel, anit, pul, para, imza, mezar...) ele.
EXCLUDE_TERMS = [
    "memorial", "statue", "monument", "bust", "stamp", "banknote", "coin",
    "signature", "grave", "tomb", "mural", "graffiti", "plaque", "logo",
    "map", "diagram", "flag", "poster", "cartoon", "caricature", "anit", "heykel",
    # belge/metin taramalari (yuz yerine yazi gosteren gorseller)
    "letter", "document", "text", "manuscript", "page", "book", "newspaper",
    "telegram", "certificate", "decree", "mektup", "belge", "imza",
    # belge/eser/antika (konu disi: eski menu, stel, lahit, antik obje)
    "menu", "dinner", "banquet", "receipt", "stela", "stele", "sarcophagus",
    "sphinx", "mummy", "papyrus", "tablet", "inscription",
    # obje/mekan (kisinin YUZU degil; muze/ev/esya fotolari)
    "museum", "muze", "park", "instrument", "rack", "shelf", "trunk", "window",
    "building", "house", "exterior", "interior", "room", "street", "villa",
    "residence", "birthplace", "office", "desk", "chair", "couch", "door",
    "gallery", "hall", "square", "cemetery", "plaque",
    # GUVENLIK: nazi sembolleri / propaganda / meme (para kazanma + hukuki risk)
    "swastika", "hakenkreuz", "ehrenb", "propaganda", "banner", "rally",
    "parade", "meeting", "ambigram", "fuck", "meme", "salute", "reich",
]


WIKI_APIS = [
    "https://tr.wikipedia.org/w/api.php",
    "https://en.wikipedia.org/w/api.php",
]


def _valid_image(ii, title_l):
    mime = ii.get("mime", "")
    if mime not in ("image/jpeg", "image/png"):
        return False
    # Kalite: kucuk/thumbnail gorseller 9:16'da bulanik/pikselli cikar.
    if ii.get("width", 0) < 640 or ii.get("height", 0) < 640:
        return False
    if any(term in title_l for term in EXCLUDE_TERMS):
        return False
    return True


def _quality_score(it, name_match):
    """KALITE puani — ALAKA'nin yanina eklenen ikincil (tie-break) sinyal.

    ONEMLI: bu puanlar bilerek KUCUK. Eskiden dikey gorsele +70, yataya -15
    veriliyordu; Pixabay'e istek 'orientation=vertical' ile gittigi icin stok
    gorseller tanim geregi dikey, arsiv fotograflari tanim geregi yataydi ->
    format farki alaka farkini eziyordu (gercek Halifax patlamasi fotografi -14,
    Kars'taki alakasiz kilise fotografi +37 aliyordu). Yatay gorsel zaten
    to_vertical ile dolguya alindigi icin kayip yalnizca kozmetik."""
    title_l = it["title"].lower()
    w, h = it["width"], it["height"]
    score = 0.0
    if name_match:
        score += 100                      # yalnizca subject-modunda verilir
    # Arsiv kaynagi (kuratorlu baslik = guvenilir alaka) stoktan onde gelsin.
    if it.get("source") in ARCHIVE_SOURCES:
        score += ARCHIVE_BONUS
    # Format tercihi: kucuk. Dikey biraz daha iyi doldurur, o kadar.
    if h >= w:
        score += 8
    elif h >= w * 0.85:
        score += 3
    else:
        score -= 4
    # Cozunurluk (megapiksel bonusu, tavan 12)
    score += min((w * h) / 1_000_000.0, 12)
    # Portre/yuz ipuclari
    if any(t in title_l for t in PORTRAIT_BONUS_TERMS):
        score += 20
    # Grup/baglam/uniforma yumusak cezasi
    for term in SOFT_PENALTY:
        if term in title_l:
            score -= 20
    return score


def _pack(ii, title, source="commons"):
    meta = ii.get("extmetadata", {})
    return {
        "url": ii["url"],
        "width": ii["width"],
        "height": ii["height"],
        "title": title,
        "license": meta.get("LicenseShortName", {}).get("value", "?"),
        "source": source,
    }


def _license_ok(lic):
    """Sadece PD / CC0 / CC-BY kabul. BY-SA / BY-NC / BY-ND / belirsiz ELE.
    (Yeni dis kaynaklara uygulanir; Wikimedia guvenilir kabul edilir.)"""
    if not lic:
        return False
    l = lic.lower()
    # Once reddedilenler: sharealike / noncommercial / noderivatives
    for bad in ("by-sa", "by-nc", "by-nd", "-sa", "-nc", "-nd",
                "sharealike", "share alike", "noncommercial", "non-commercial",
                "noderiv", "no deriv"):
        if bad in l:
            return False
    # Kabul edilenler
    for good in ("cc0", "cc-zero", "public domain", "publicdomain", "pdm",
                 "no known copyright", "no restrictions",
                 "cc-by", "cc by", "attribution", "pexels", "pixabay"):
        if good in l:
            return True
    if l.strip() == "by":            # Openverse kisa kodu
        return True
    return False


# Alaka olcumunde ATLANACAK jenerik/stil kelimeleri (ayirt edici degil).
_REL_STOP = {
    "painting", "portrait", "photo", "photograph", "image", "picture", "the",
    "and", "with", "dark", "classical", "vintage", "style", "scene", "cinematic",
    "closeup", "shadow", "chiaroscuro", "baroque", "expression", "dramatic",
    # jenerik/rol sozcukleri (ayirt edici degil -> yanlis eslesme uretir)
    "actor", "actress", "actors", "film", "movie", "series", "show", "star",
    "building", "man", "woman", "people", "group",
}


# Kaynak siniflari: arsiv (kuratorlu, gercek olay/kisi gorseli) vs stok
# (jenerik, etiketleri gurultulu). Spesifik bir beat'te stok kullanilmamali.
ARCHIVE_SOURCES = {"wikipedia", "commons", "openverse", "met", "rijksmuseum"}
STOCK_SOURCES = {"pixabay", "pexels"}

# ALAKA agirliklari: kalite puanindan (yaklasik -60..+130 araligi) BUYUK olsun ki
# "dogru gorsel" her zaman "guzel gorsel"i yensin.
REL_PROPER = 300        # ozel isim/yil eslesmesi: Halifax, Voyager, Gallipoli, 1917
REL_PLAIN = 60          # jenerik ama ayirt edici kelime: ruins, spacecraft, trench
REL_YEAR = 120          # yil eslesmesi: destekleyici sinyal, tek basina yetmez
ARCHIVE_BONUS = 30      # esitlikte arsiv kaynagi one gecsin
OFF_TOPIC_DEMOTE = -500      # arsiv kaynagi hic tutmadi -> son care olarak dipte dursun
STOCK_OFF_TOPIC_DEMOTE = -10000   # stok kaynak spesifik beat'i tutmadi -> pratikte kullanma


def _term_tokens(term):
    """Sorguyu ikiye ayirir: (ozel_isimler, jenerik_ayirt_ediciler).

    Ozel isim = buyuk harfle baslayan kelime ya da 4 haneli yil. Bunlar bir stok
    fotografin etiketinde tesadufen bulunmaz, o yuzden ASIL alaka sinyalidir:
    'Halifax Explosion 1917 ruins' sorgusunda belirleyici olan 'ruins' degil,
    'Halifax'/'Explosion'/'1917'."""
    proper, plain = [], []
    for w in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]+|\d{4}", term or ""):
        if w.isdigit():
            proper.append(w)
            continue
        wl = w.lower()
        if len(wl) < 4 or wl in _REL_STOP:
            continue
        (proper if w[0].isupper() else plain).append(wl)
    return proper, plain


WIKI_PAGE_BASE = 40     # konunun kendi Wikipedia sayfasindaki gorsel: konu-garantili


def _core_term(term):
    """Sorgunun ozel isimlerinden kisa bir CEKIRDEK arama terimi uretir.

    Commons aramasi anahtar-kelime bazlidir: "Halifax ruined houses after
    explosion 1917" gibi dogal dil sorgulari neredeyse hic sonuc dondurmuyor
    (olcum: beat basina 1 aday), bu yuzden ayni fotograf 8 beat'te tekrarlaniyordu.
    Cekirdek terim ("Halifax Explosion 1917") olayin tum arsivini havuza getirir;
    siralama yine ORIJINAL sorguya gore yapildigi icin beat'e ozel foto one cikar."""
    proper, _ = _term_tokens(term)
    core = " ".join(proper)
    return core if core and core.lower() != (term or "").lower() else None


def _word_in(title_l, w):
    """TAM KELIME, ya da >=6 harfli kelimeler icin 6-harf govde onegi
    (machiavelli~machiauello gibi dil/donem varyantlarini yakalar)."""
    if re.search(r"\b" + re.escape(w) + r"\b", title_l):
        return True
    return len(w) >= 6 and w[:6] in title_l


def _relevance(title_l, term, stock=False):
    """Sorgunun kac ayirt edici kelimesi baslikta geciyor -> ALAKA puani.
    Doner: (puan, ozel_isim_eslesme_sayisi, sorguda_ozel_isim_var_mi).

    STOK KAYNAK KURALI: Pixabay/Pexels basligi bir etiket torbasidir, kelimeler
    birbirinden bagimsizdir -> ozel isim eslesmesi tesaduf olabilir. Gercek vaka:
    "SS Mont-Blanc French cargo ship" sorgusu, "cake, mont blanc, french, dessert"
    etiketli bir PASTA fotografini 900 puanla birinci yapti. Bu yuzden stok
    kaynaklar ozel isim puanini ancak sorgudaki ozel isimlerin TAMAMI eslesirse
    alir ("PlayStation 3 console" gibi tek isimli sorgular calismaya devam eder);
    aksi halde yalnizca jenerik kelimelerden (atmosfer degeri) puan alirlar."""
    proper, plain = _term_tokens(term)
    names = [w for w in proper if not w.isdigit()]
    years = [w for w in proper if w.isdigit()]
    hit_n = sum(1 for w in names if _word_in(title_l, w))
    hit_y = sum(1 for w in years if _word_in(title_l, w))
    hit_x = sum(1 for w in plain if _word_in(title_l, w))
    if stock and names and hit_n < len(names):
        hit_n = 0
    # YIL TEK BASINA ALAKA DEGILDIR: "SS Imo ship 1917" sorgusuna yalnizca 1917'yi
    # tutan "Caroline Foster, 1917.jpg" gelmisti. Yil ancak bir isim/kelime
    # eslesmesinin YANINDA puan getirir.
    if hit_n == 0 and hit_x == 0:
        hit_y = 0
    return (hit_n * REL_PROPER + hit_y * REL_YEAR + hit_x * REL_PLAIN,
            hit_n, bool(names))


def _term_relevant(title_l, term):
    """Sorgunun AYIRT EDICI kelimelerinden en az biri baslikta geciyor mu?
    Eslesme: TAM KELIME, ya da >=6 harfli ayirt edici kelimeler icin 6-harf
    govde onegi (machiavelli~machiauello, narcissus~narciso gibi eski/dil
    varyantlarini yakalar; ama 'spader'~'spade' gibi kisa-govde yanlis
    pozitiflerini ONLER). Ayirt edici kelime yoksa (yalniz stil) -> notr."""
    words = [w for w in re.findall(r"[a-zçğıöşü]+", (term or "").lower())
             if len(w) >= 4 and w not in _REL_STOP]
    if not words:
        return True
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", title_l):
            return True
        if len(w) >= 6 and w[:6] in title_l:
            return True
    return False


def _dims(it):
    """Boyut bilinmiyorsa notr varsayilan (indirme sonrasi to_vertical zaten olcekler)."""
    w = it.get("width") or 0
    h = it.get("height") or 0
    if w < 1 or h < 1:
        return 1024, 1024
    return w, h


def resolve_wiki_title(api, subject):
    """Konu icin en iyi Wikipedia sayfa basligini bulur."""
    try:
        r = requests.get(api, params={
            "action": "query", "format": "json",
            "list": "search", "srsearch": subject, "srlimit": 1,
        }, headers=HEADERS, timeout=20)
        hits = r.json().get("query", {}).get("search", [])
        return hits[0]["title"] if hits else None
    except Exception:
        return None


def wikipedia_page_images(api, subject):
    """Kisinin Wikipedia sayfasinda kullanilan gorseller = o kisinin GERCEK fotolari."""
    title = resolve_wiki_title(api, subject)
    if not title:
        return []
    try:
        r = requests.get(api, params={
            "action": "query", "format": "json",
            "generator": "images", "titles": title, "gimlimit": 50,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        }, headers=HEADERS, timeout=30)
        pages = r.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    out = []
    for p in pages.values():
        ii_list = p.get("imageinfo")
        if not ii_list:
            continue
        t = p.get("title", "")
        if _valid_image(ii_list[0], t.lower()):
            out.append(_pack(ii_list[0], t, source="wikipedia"))
    return out


def commons_search_images(subject, limit=40):
    """Commons File aramasi (tamamlayici kaynak)."""
    try:
        r = requests.get(COMMONS_API, params={
            "action": "query", "format": "json",
            "generator": "search", "gsrsearch": subject,
            "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        }, headers=HEADERS, timeout=30)
        pages = r.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    out = []
    for p in pages.values():
        ii_list = p.get("imageinfo")
        if not ii_list:
            continue
        t = p.get("title", "")
        if _valid_image(ii_list[0], t.lower()):
            out.append(_pack(ii_list[0], t, source="commons"))
    return out


# ---- KATMAN 1: ek telifsiz/acik kaynaklar (soyut konular icin guclu) ----
# Hepsi ayni sema ile doner: {url,width,height,title,license,source}.
# Lisans _license_ok'tan gecmezse cagiran taraf eler.

def openverse_search(query, limit=20):
    """Openverse: CC arama motoru (keysiz). license=cc0,pdm,by ile on-filtre."""
    try:
        r = requests.get("https://api.openverse.org/v1/images/", params={
            "q": query, "license": "cc0,pdm,by",
            "page_size": limit, "mature": "false",
        }, headers=HEADERS, timeout=25)
        results = r.json().get("results", []) or []
    except Exception:
        return []
    out = []
    for it in results:
        url = it.get("url")
        if not url:
            continue
        lic = it.get("license", "") or ""
        ver = it.get("license_version", "") or ""
        out.append({
            "url": url, "width": it.get("width") or 0, "height": it.get("height") or 0,
            "title": it.get("title", "") or "", "license": f"CC {lic} {ver}".strip(),
            "source": "openverse",
        })
    return out


def met_search(query, limit=8):
    """Metropolitan Museum Open Access (keysiz). Sadece isPublicDomain."""
    try:
        r = requests.get(
            "https://collectionapi.metmuseum.org/public/collection/v1/search",
            params={"q": query, "hasImages": "true"}, headers=HEADERS, timeout=25)
        ids = (r.json().get("objectIDs") or [])[:limit * 3]
    except Exception:
        return []
    out = []
    for oid in ids:
        if len(out) >= limit:
            break
        try:
            o = requests.get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}",
                headers=HEADERS, timeout=20).json()
        except Exception:
            continue
        if not o.get("isPublicDomain"):
            continue
        img = o.get("primaryImage") or ""
        if not img:
            continue
        out.append({
            "url": img, "width": 0, "height": 0,
            "title": o.get("title", "") or "", "license": "Public domain",
            "source": "met",
        })
    return out


def rijksmuseum_search(query, limit=12):
    """Rijksmuseum (RIJKS_API_KEY gerekli). Tum sonuclar public domain."""
    key = os.environ.get("RIJKS_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://www.rijksmuseum.nl/api/en/collection", params={
            "key": key, "q": query, "imgonly": "true", "ps": limit,
        }, headers=HEADERS, timeout=25)
        arts = r.json().get("artObjects", []) or []
    except Exception:
        return []
    out = []
    for a in arts:
        wi = a.get("webImage") or {}
        url = wi.get("url")
        if not url:
            continue
        out.append({
            "url": url, "width": wi.get("width") or 0, "height": wi.get("height") or 0,
            "title": a.get("title", "") or "", "license": "Public domain",
            "source": "rijksmuseum",
        })
    return out


def pexels_search(query, limit=10):
    """Pexels (PEXELS_API_KEY gerekli). Soyut doku/atmosfer icin.
    Pexels lisansi ticari+atifsiz serbest -> havuzda 'pexels' etiketiyle kabul."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://api.pexels.com/v1/search", params={
            "query": query, "per_page": limit, "orientation": "portrait",
        }, headers={"Authorization": key}, timeout=25)
        photos = r.json().get("photos", []) or []
    except Exception:
        return []
    out = []
    for p in photos:
        src = (p.get("src") or {})
        url = src.get("portrait") or src.get("large2x") or src.get("original")
        if not url:
            continue
        out.append({
            "url": url, "width": p.get("width") or 0, "height": p.get("height") or 0,
            "title": p.get("alt", "") or "", "license": "Pexels",
            "source": "pexels",
        })
    return out


def pixabay_search(query, limit=10):
    """Pixabay (PIXABAY_API_KEY gerekli). Soyut doku/atmosfer icin."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://pixabay.com/api/", params={
            "key": key, "q": query, "image_type": "photo",
            "orientation": "vertical", "per_page": limit, "safesearch": "true",
        }, headers=HEADERS, timeout=25)
        hits = r.json().get("hits", []) or []
    except Exception:
        return []
    out = []
    for h in hits:
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        out.append({
            "url": url, "width": h.get("imageWidth") or 0, "height": h.get("imageHeight") or 0,
            "title": h.get("tags", "") or "", "license": "Pixabay",
            "source": "pixabay",
        })
    return out


# TODO(kaynak): Library of Congress / NYPL Digital Collections.
# LoC (loc.gov ?fo=json) ve NYPL lisans/rights alanlari tutarsiz; "sadece
# PD/CC0/CC-BY" kuralini guvenli uygulamak icin item-bazli rights dogrulamasi
# gerekiyor (NYPL ayrica token ister). Guvenli parse yazilana kadar havuza
# DAHIL EDILMIYOR (belirsiz lisansli gorsel almamak icin bilincli tercih).

EXTERNAL_CONNECTORS = [
    openverse_search, met_search, rijksmuseum_search, pexels_search, pixabay_search,
]


def search_images(subject, scene_count, queries=None, kind="specific"):
    """
    Coklu kaynak havuzu (alaka + cesitlilik + soyut konu destegi):
    1) Kisinin Wikipedia sayfa gorselleri (tr+en) -> gercek fotolari, guvenilir
    2) Wikimedia Commons aramasi -> guvenilir (telifsiz varsayilir)
    3) Dis kaynaklar (Openverse/Met/Rijks/Pexels/Pixabay) -> SADECE PD/CC0/CC-BY

    queries verilirse (somut arama terimleri, orn. "Caravaggio Narcissus painting"),
    Commons + dis kaynaklar bu terimlerle taranir ve isim-eslesme sarti aranmaz
    (terimin kendisi alaka sinyalidir). Yoksa 'subject' kullanilir ve Commons'ta
    soyisim-eslesme sarti korunur (alakasiz tablo/eşya elenir).

    SIRALAMA ALAKA-ONCELIKLIDIR: puan = _relevance (sorgunun ozel isimlerini
    tutma derecesi) + _quality_score (format/cozunurluk, kucuk tie-break).
    Boylece "Mustafa Kemal in Gallipoli.jpg", dikey ve yuksek cozunurluklu ama
    alakasiz bir stok fotografinin onune gecer.
    """
    surname = subject.strip().split()[-1].lower() if subject.strip() else ""
    search_terms = [q for q in (queries or []) if q and q.strip()] or [subject]
    explicit = bool(queries)
    seen = set()
    merged = []

    def add(items, trusted, require_name, term):
        for it in items:
            u = it.get("url")
            if not u or u in seen:
                continue
            title_l = it.get("title", "").lower()
            name_match = bool(surname and surname in title_l)
            # KONU-DISI ELEME: TUM kaynaklara (dis kaynaklar _valid_image'dan
            # gecmiyordu -> stel/lahit/menu gibi alakasizlar siziyordu).
            if any(term in title_l for term in EXCLUDE_TERMS):
                continue
            # LISANS: TUM kaynaklara uygulanir -> sadece PD/CC0/CC-BY.
            # BY-SA/BY-NC/BY-ND ve belirsiz ("?") ELE (para kazanma + hukuki risk).
            if not _license_ok(it.get("license", "")):
                continue
            # subject-modu Commons'ta isim eslesmesi sarti (junk eler); explicit
            # query modunda terim yeterince spesifik -> sart yok.
            if require_name and not name_match:
                continue
            it.setdefault("width", 0)
            it.setdefault("height", 0)
            w, h = _dims(it)
            it["width"], it["height"] = w, h
            # ALAKA-ONCELIKLI SIRALAMA (kelime-gorsel uyumunun kalbi):
            # puan = ALAKA + kalite. Alaka agirliklari kaliteden buyuk oldugu icin
            # "sorguyu tutan" gorsel, "daha guzel duran" gorseli her zaman yener.
            is_stock = it["source"] in STOCK_SOURCES
            # GENERIC beat = atmosfer beat'i. Orada stok kaynaga ozel isim
            # katiligi UYGULANMAZ: 'Chateau d'If prison cell dark' gibi bir
            # atmosfer sorgusunda hicbir stok foto 'chateau' etiketi tasimaz ->
            # havuz tamamen bosaliyor ve komsu gorsel 6 beat boyunca tekrarlaniyordu
            # (Monte Cristo videosunda olculdu: 11 beat, 6 farkli gorsel).
            strict = is_stock and kind == "specific"
            rel, proper_hits, has_proper = _relevance(title_l, term, stock=strict)
            if it["source"] == "wikipedia":
                # Konunun kendi Wikipedia sayfasindaki gorsel: sorguyu tutmasa da
                # KONUYA aittir (olayin gercek fotografi). Kucuk bir taban puan ver
                # -> havuzda kalir ama sorguyu tutan gorselin onune gecmez.
                rel = max(rel, WIKI_PAGE_BASE)
            elif strict and has_proper and proper_hits == 0:
                # Sorguda ozel isim var (spesifik an) ama stok gorselde yok ->
                # gercek arsiv gorseli dururken Kars'tan kilise fotografi gelmesin.
                rel += STOCK_OFF_TOPIC_DEMOTE
            elif rel == 0:
                if is_stock:
                    continue                # hicbir kelime tutmayan stok: havuza girmesin
                rel += OFF_TOPIC_DEMOTE     # arsiv: elenmez, son care olarak dipte kalir
            seen.add(u)
            it["_score"] = rel + _quality_score(it, name_match and not explicit)
            it["_rel"] = rel                  # teshis/rapor icin
            it["_term"] = term                # cesitlilik icin hangi sorgudan geldi
            merged.append(it)

    # 1) Wikipedia kisi sayfasi (yalniz subject; guvenilir)
    for api in WIKI_APIS:
        add(wikipedia_page_images(api, subject), trusted=True,
            require_name=False, term=search_terms[0])

    # 2) Commons + 3) dis kaynaklar: her arama terimi icin
    for term in search_terms:
        add(commons_search_images(term, limit=max(40, scene_count * 5)),
            trusted=True, require_name=not explicit, term=term)
        # Havuzu genislet: uzun dogal-dil sorgusu Commons'ta sonuc vermez ->
        # ozel isimlerden olusan cekirdek terimle olayin arsivini de tara.
        core = _core_term(term) if explicit else None
        if core:
            add(commons_search_images(core, limit=max(40, scene_count * 5)),
                trusted=True, require_name=False, term=term)
        for connector in EXTERNAL_CONNECTORS:
            try:
                add(connector(term), trusted=False, require_name=False, term=term)
            except Exception:
                continue

    merged.sort(key=lambda x: x["_score"], reverse=True)
    return _diversify(merged)


def _diversify(cands):
    """Sorgu (kavram) bazli round-robin: ilk sira yine global en iyi (hook
    karesi), ama sonrasi kavramlar arasi donusumlu -> tek konuya yigilmaz.
    Karanlik Uclu'de narsisizm/makyavelizm/psikopati gorselleri dengeli dagilir."""
    from collections import OrderedDict
    groups = OrderedDict()
    for c in cands:
        groups.setdefault(c.get("_term", ""), []).append(c)
    # her grup zaten skorla sirali (merged sirali geldi)
    ordered = []
    while any(groups.values()):
        # bu turda gruplari, mevcut en iyi elemanlarinin skoruna gore sirala
        heads = sorted(
            (g[0]["_score"], k) for k, g in groups.items() if g)
        for _, k in reversed(heads):
            if groups[k]:
                ordered.append(groups[k].pop(0))
    return ordered


def to_vertical(src_path, dst_path):
    """Fotografi 768x1344 dikeye cevirir: arkada bulanik dolgu, onde tam foto (yuz kirpilmaz)."""
    vf = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},boxblur=24:4[bg];"
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    cmd = ["ffmpeg", "-y", "-i", src_path, "-filter_complex", vf,
           "-frames:v", "1", dst_path]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.returncode == 0, res.stderr[-800:]


# ---- KATMAN 2: uretim fallback (soyut konu / zayif havuz) ----
# Havuz bos ya da en iyi puan esigin altindaysa sahneye ozel gorsel URET.
GEN_MIN_SCORE = 25          # havuzdaki en iyi puan bunun altindaysa "zayif"


def _gen_prompt(narration, term):
    """Uretim promptu narration'dan turetilir; 9:16 dikey, karanlik sinematik."""
    base = (narration or term or "").strip()
    words = base.split()
    if len(words) > 16:
        base = " ".join(words[:16])
    return (f"{base}, vertical 9:16 composition, dark cinematic tone, "
            f"dramatic lighting, high detail, no text, no watermark")


def generate_scene_image(prompt, out_path):
    """Sahneye ozel gorsel uret. Arayuz soyut; kurulu bir uretici varsa kullan.

    Su an lokal bir uretici KURULU DEGIL (ComfyUI/SDXL 'gercek foto' karariyla
    dusuruldu). Kurulunca asagidaki dallardan biri doldurulacak; fallback
    KALICI olsun diye arayuz burada hazir duruyor.

    Doner: (ok: bool, source_or_reason: str)
    """
    # 1) Bir image-gen API anahtari tanimliysa (env) onu kullan. TODO: entegrasyon.
    if os.environ.get("IMAGE_GEN_API_KEY"):
        # TODO: secili API'ye istek at, sonucu out_path'e 768x1344 yaz.
        return False, "gen-api-todo"
    # 2) Lokal ComfyUI (5070) ayakta mi? (dusuruldu ama ileride acilabilir)
    try:
        requests.get("http://localhost:5070/", timeout=2)
        # TODO: comfy_server'a 9:16 karanlik-sinematik workflow gonder.
        return False, "comfy-todo"
    except Exception:
        pass
    return False, "no-generator"


def _download_vertical(candidates, dest, skip_urls=None):
    """Aday listesinden ilk indirilebilen+9:16'ya cevrilebilen gorseli dest'e
    yazar. Basarirsa kullanilan adayin meta'sini, olmazsa None doner.

    skip_urls: onceki beat'lerde KULLANILMIS gorseller. Once bunlar atlanir
    (ayni fotografin 6 beat boyunca tekrarlanmasi videoyu bozuk gosteriyordu);
    baska hicbir aday kalmazsa ikinci turda tekrara izin verilir."""
    if skip_urls:
        fresh = [c for c in candidates if c.get("url") not in skip_urls]
        meta = _download_vertical(fresh, dest) if fresh else None
        if meta:
            return meta
    for cand in candidates:
        try:
            resp = requests.get(cand["url"], headers=HEADERS, timeout=60)
            if resp.status_code != 200 or len(resp.content) < 10000:
                continue
            fd, tmp = tempfile.mkstemp(suffix=".img")
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
            ok, _ = to_vertical(tmp, dest)
            os.remove(tmp)
            if not ok:
                continue
            return {
                "title": cand.get("title", ""), "license": cand.get("license", "?"),
                "source": cand.get("source", "wikimedia"), "source_url": cand["url"],
                # beat raporu icin: bu gorsel sorguyu ne kadar tuttu?
                "relevance": round(cand.get("_rel", 0), 1),
                "score": round(cand.get("_score", 0), 1),
            }
        except Exception:
            continue
    return None


def _clip_rank(image_paths, visual_text, timeout=30):
    """CLIP re-ranker: gorsel listesini visual_text ile karsilastir, sirali don.
    Basarisizsa (CLIP calismiyorsa) orijinal sirayi koru."""
    # DOSYA KOLPASI (2026-10-15): acilan dosyalar sadece POST BASARILI olursa
    # kapaniyordu. CLIP sunucusu kapali oldugunda (5004) istek hata firlatiyor,
    # tutamaklar acik kaliyor ve Windows acik dosyayi yeniden adlandirmaya izin
    # vermedigi icin kazanan gorsel scene_N.png'ye tasinamiyordu (WinError 32 ->
    # /fetch_scenes 500). Kapatma artik finally'de.
    files = []
    try:
        for p in image_paths:
            files.append(("images", (os.path.basename(p), open(p, "rb"), "image/png")))
        resp = requests.post("http://localhost:5004/rank",
                             data={"query": visual_text}, files=files,
                             timeout=timeout)
        if resp.status_code == 200:
            ranked = resp.json().get("ranked", [])
            if ranked:
                return ranked
    except Exception:
        pass
    finally:
        for _, f in files:
            try:
                f[1].close()
            except Exception:
                pass
    # fallback: orijinal sirayi koru
    return [{"index": i, "score": 0} for i in range(len(image_paths))]


def _fetch_scenes_per_query(subject, queries, narration, kinds=None, anchor=None, visuals=None):
    """BEAT-SENKRON: her query (beat) icin O query'ye en alakali TEK gorseli
    indirir; sonuc queries ile 1:1 hizalidir (i. gorsel i. beat'e aittir).
    Bir beat icin gorsel bulunamazsa komsu beat'in gorseli dongulenir.

    kinds: beat basina "specific" | "generic".
    anchor: konunun Ingilizce ozel adi ("Halifax Explosion 1917").
    visuals: beat basina CLIP icin gorsel tarifleri (ozel isimsiz, kisa, somut).

    SPESIFIK beat'lerde secilen gorselin KONUYU da tutmasi zorunludur: "SS
    Mont-Blanc cargo ship" sorgusu, capa olmadan Fransa'daki Mont Blanc DAGININ
    1787 gravurunu getiriyordu (isim tutuyor, konu tutmuyor). Capayi tutan aday
    yoksa beat bos birakilir -> komsudan konuya ait bir gorsel dongulenir."""
    N_CANDIDATES = 5  # her beat icin kac aday indir + CLIP ile sirala
    images, used, src_counts = [None] * len(queries), [None] * len(queries), {}
    seen_urls = set()           # ayni gorsel birden fazla beat'e gitmesin
    pools = []

    # KONU HAVUZU: capa terimiyle bir kez aranir; spesifik bir beat'in kendi
    # sorgusu konuyu tutan aday bulamazsa buradan (konuya ait gercek arsiv)
    # secilir. Beat basina degil, video basina tek arama -> hizli.
    topic_pool = []
    if anchor:
        try:
            topic_pool = [c for c in search_images(subject, 3, queries=[anchor])
                          if c.get("_rel", 0) > 0]
        except Exception:
            topic_pool = []

    for i, q in enumerate(queries):
        try:
            cands = search_images(subject, N_CANDIDATES, queries=[q],
                                  kind=(kinds[i] if kinds and i < len(kinds) else "specific"))
        except Exception:
            cands = []
        pools.append(cands)
        # ALAKASIZ ADAY KULLANILMAZ. Puani negatif olanlar (stok/arsiv dolgusu)
        # ve sorguyu hic tutmayanlar (rel=0) elenir. Beat bos kalirsa asagidaki
        # komsu-doldurma devreye girer: ALAKALI bir fotografi tekrar gostermek,
        # alakasiz bir fotograf (pasta, bale tablosu) gostermekten iyidir.
        alakali = [c for c in cands if c.get("_rel", 0) > 0]

        # CAPA SARTI: spesifik beat'te gorsel konuyu da tutmali.
        kind = (kinds[i] if kinds and i < len(kinds) else "specific")
        if anchor and kind == "specific":
            alakali = [c for c in alakali
                       if _relevance(c["title"].lower(), anchor)[0] > 0] or topic_pool
        elif kind == "generic":
            # Atmosfer beat'inde stok serbest, ama TEK kelime eslesmesi yeterli
            # degil: "alien contact sci-fi concept" sorgusuna tek kelimeyle
            # gotik fantezi portresi geliyordu. En az iki sinyal iste.
            alakali = [c for c in alakali
                       if c.get("_rel", 0) >= 2 * REL_PLAIN
                       or c["source"] in ARCHIVE_SOURCES] or alakali

        # CLIP RE-RANKER: N aday indir, CLIP ile sirala, kazanani kullan.
        if alakali and visuals and i < len(visuals):
            clip_text = visuals[i]
            temp_paths = []
            temp_meta = []
            # TEKRAR ENGELI (2026-10-15): CLIP yolu seen_urls'i hic dikkate
            # almiyordu -> ayni gorsel ("Ottoman dynasty.jpg") 4 beat'te birden
            # cikti. Once daha once kullanilmamis adaylar; hepsi kullanilmissa
            # eski havuza izin ver (beat bos kalmasin).
            taze = [c for c in alakali if c.get("url") not in seen_urls] or alakali
            for ci, cand in enumerate(taze[:N_CANDIDATES]):
                try:
                    resp = requests.get(cand["url"], headers=HEADERS, timeout=60)
                    if resp.status_code != 200 or len(resp.content) < 10000:
                        continue
                    fd, tmp = tempfile.mkstemp(suffix=".img")
                    with os.fdopen(fd, "wb") as f:
                        f.write(resp.content)
                    dest_tmp = os.path.join(ASSETS_DIR, f"_clip_cand_{i}_{ci}.png")
                    ok, _ = to_vertical(tmp, dest_tmp)
                    os.remove(tmp)
                    if ok:
                        temp_paths.append(dest_tmp)
                        temp_meta.append(cand)
                except Exception:
                    continue

            if temp_paths:
                ranked = _clip_rank(temp_paths, clip_text)
                # Kazanani sec (en yuksek CLIP skoru)
                if ranked:
                    best = max(ranked, key=lambda x: x.get("score", 0))
                    best_idx = best.get("index", 0)
                    if best_idx < len(temp_meta):
                        dest = os.path.join(ASSETS_DIR, f"scene_{i}.png")
                        os.replace(temp_paths[best_idx], dest)
                        # Kalan adaylari temizle
                        for tp in temp_paths:
                            if os.path.exists(tp) and tp != dest:
                                os.remove(tp)
                        # META'yi _download_vertical ile AYNI semada kur. Eskiden
                        # ham aday sozlugu veriliyordu; icinde relevance/source_url
                        # olmadigi icin beat raporu her beat'e ALAKA=0 yaziyor ve
                        # "1 farkli gorsel" diyordu (kalite olcumu korlesmisti).
                        kazanan = temp_meta[best_idx]
                        used[i] = {
                            "title": kazanan.get("title", ""),
                            "license": kazanan.get("license", "?"),
                            "source": kazanan.get("source", "?"),
                            "source_url": kazanan.get("url", ""),
                            "relevance": round(kazanan.get("_rel", 0), 1),
                            "score": round(kazanan.get("_score", 0), 1),
                            "kind": kind,
                            "clip_score": best.get("score", 0),
                        }
                        images[i] = dest
                        seen_urls.add(kazanan.get("url", ""))
                        src_counts[kazanan["source"]] = src_counts.get(kazanan["source"], 0) + 1
                        continue  # CLIP basarili, bir sonraki beat'e gec
                # CLIP basarisizsa veya aday yoksa, gecici dosyalari temizle
                for tp in temp_paths:
                    if os.path.exists(tp):
                        os.remove(tp)

        # CLIP yoksa veya basarisizsa: eski davranis (tek gorsel indir)
        dest = os.path.join(ASSETS_DIR, f"scene_{i}.png")
        meta = _download_vertical(alakali, dest, skip_urls=seen_urls)
        if meta:
            images[i] = dest
            used[i] = dict(meta, kind=kind)
            seen_urls.add(meta.get("source_url") or meta.get("url", ""))
            src_counts[meta["source"]] = src_counts.get(meta["source"], 0) + 1

    # Son care: hicbir beat'e alakali gorsel gelmediyse (cok zayif konu) eski
    # davranisa don, video hic uretilmemesindense en iyi adaylarla uretilsin.
    if not any(images):
        for i, cands in enumerate(pools):
            dest = os.path.join(ASSETS_DIR, f"scene_{i}.png")
            meta = _download_vertical(cands, dest, skip_urls=seen_urls)
            if meta:
                images[i] = dest
                used[i] = dict(meta, weak=True)
                seen_urls.add(meta["source_url"])
                src_counts[meta["source"]] = src_counts.get(meta["source"], 0) + 1
    # bos beat'leri komsudan doldur (alakasiz dolgu yerine en yakin alakali)
    if any(im is None for im in images) and any(im for im in images):
        n = len(images)
        for i in range(n):
            if images[i]:
                continue
            j = next((k for d in range(1, n) for k in (i - d, i + d)
                      if 0 <= k < n and images[k]), None)
            if j is not None:
                images[i] = images[j]
                used[i] = dict(used[j] or {}, reused=True)
                src_counts["reused"] = src_counts.get("reused", 0) + 1
    return images, used, src_counts


@app.route('/fetch_scenes', methods=['POST'])
def fetch_scenes():
    data = request.get_json(silent=True) or {}
    subject = data.get('subject')
    scene_count = data.get('scene_count')
    # Soyut konular icin somut arama terimleri (orn. "Caravaggio Narcissus painting").
    queries = data.get('queries') or data.get('image_query')
    if isinstance(queries, str):
        queries = [queries]
    narration = data.get('narration', '')
    per_query = bool(data.get('per_query'))

    if not subject:
        return jsonify({"error": "subject gerekli (ornek: 'Mustafa Kemal Ataturk')"}), 400

    # BEAT-SENKRON YOL: her query'ye 1:1 hizali gorsel (gorsel-anlatim uyumu)
    if per_query and queries:
        for pat in ("scene_*.png",):
            for p in glob.glob(os.path.join(ASSETS_DIR, pat)):
                os.remove(p)
        try:
            images, used, src_counts = _fetch_scenes_per_query(
                subject, queries, narration,
                kinds=data.get('kinds'), anchor=data.get('anchor'),
                visuals=data.get('visuals'))
        except Exception as e:
            print(f"[fetch_scenes:per_query] HATA: {e}", flush=True)
            import traceback; traceback.print_exc()
            return jsonify({"error": f"Gorsel isleme hatasi: {e}",
                            "per_query": True}), 500
        if not any(images):
            return jsonify({"error": f"'{subject}' icin hicbir beat'e gorsel bulunamadi",
                            "per_query": True}), 404
        print(f"[fetch_scenes:per_query] subject={subject!r} beats={len(queries)} "
              f"kaynak={src_counts}")
        return jsonify({
            "success": True, "subject": subject, "per_query": True,
            "scene_count": len(images), "images": images,
            "sources": used, "source_breakdown": src_counts,
        })

    try:
        scene_count = int(scene_count)
    except (TypeError, ValueError):
        return jsonify({"error": "scene_count sayi olmali"}), 400
    if scene_count < 1:
        scene_count = 1

    try:
        candidates = search_images(subject, scene_count, queries=queries)
    except Exception as e:
        return jsonify({"error": f"Gorsel aramasi basarisiz: {e}"}), 500

    top_score = candidates[0]["_score"] if candidates else -999
    weak_pool = (not candidates) or (top_score < GEN_MIN_SCORE)

    images = []
    used = []
    src_counts = {}     # kaynak -> kac gorsel KULLANILDI
    idx = 0
    for cand in candidates:
        if len(images) >= scene_count:
            break
        try:
            resp = requests.get(cand["url"], headers=HEADERS, timeout=60)
            if resp.status_code != 200 or len(resp.content) < 10000:
                continue
            fd, tmp = tempfile.mkstemp(suffix=".img")
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
            dest = os.path.join(ASSETS_DIR, f"scene_{idx}.png")
            ok, err = to_vertical(tmp, dest)
            os.remove(tmp)
            if not ok:
                continue
            images.append(dest)
            src = cand.get("source", "wikimedia")
            src_counts[src] = src_counts.get(src, 0) + 1
            used.append({
                "title": cand["title"],
                "license": cand["license"],
                "source": src,
                "source_url": cand["url"],
            })
            idx += 1
        except Exception:
            continue

    # KATMAN 2: eksik sahneler icin (yetersiz gercek gorsel ya da zayif havuz) URET.
    generated = 0
    gen_reason = None
    missing = scene_count - len(images)
    if missing > 0 or weak_pool:
        prompt = _gen_prompt(narration, (queries or [subject])[0])
        for _ in range(max(missing, 0)):
            dest = os.path.join(ASSETS_DIR, f"scene_{idx}.png")
            ok, reason = generate_scene_image(prompt, dest)
            gen_reason = reason
            if not ok:
                break              # uretici yok -> deneme, eldeki gercek gorsellerle devam
            images.append(dest)
            src_counts["generated"] = src_counts.get("generated", 0) + 1
            used.append({"title": "generated", "license": "generated",
                         "source": "generated", "source_url": None})
            generated += 1
            idx += 1

    # TEMPO KORUMA: yeterli ALAKALI gercek gorsel yok + uretici de yoksa,
    # alakasiz dolgu yerine mevcut alakali gorselleri DONGUYLE tekrar kullan.
    # (Telifli modern konularda -Blacklist gibi- serbest gorsel az; birkac
    #  gercek fotoyu farkli Ken Burns ile tekrar gostermek, alakasiz tablo
    #  gostermekten iyidir.) Bitisik tekrar olmasin diye kaydirmali doldururuz.
    reused = 0
    if 0 < len(images) < scene_count and generated == 0:
        base = list(images)
        i = 0
        while len(images) < scene_count:
            images.append(base[i % len(base)])
            i += 1
            reused += 1
        src_counts["reused"] = reused

    if not images:
        # Ne gercek gorsel var ne de uretim -> net hata (soyut konu + uretici yok)
        return jsonify({
            "error": f"'{subject}' icin gorsel bulunamadi ve uretim yok",
            "weak_pool": weak_pool, "gen_reason": gen_reason,
        }), 404

    # LOG: kaynak dokumu (stdout)
    print(f"[fetch_scenes] subject={subject!r} queries={queries} "
          f"top_score={top_score:.0f} weak={weak_pool} "
          f"kaynak_dokumu={src_counts} generated={generated} gen_reason={gen_reason}")

    return jsonify({
        "success": True,
        "subject": subject,
        "scene_count": len(images),
        "images": images,
        "sources": used,            # lisans + kaynak (video aciklamasina atif)
        "source_breakdown": src_counts,
        "generated": generated,
        "gen_reason": gen_reason,
        "weak_pool": weak_pool,
        "top_score": round(top_score, 1),
    })


# =========================================================================
#  VIDEO KLIP KATMANI: statik foto yerine GERCEK hareketli klip.
#  Kaynaklar telifsiz/serbest: Pexels Video + Pixabay Video (ucretsiz key).
#  ffmpeg_server /render_reels_video ile birlikte kullanilir.
# =========================================================================

def pexels_video_search(query, limit=6):
    """Pexels Video (PEXELS_API_KEY). Dikey tercihli, HD klip linkleri."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://api.pexels.com/videos/search", params={
            "query": query, "per_page": limit, "orientation": "portrait", "size": "medium",
        }, headers={"Authorization": key}, timeout=25)
        vids = r.json().get("videos", []) or []
    except Exception:
        return []
    out = []
    for v in vids:
        files = v.get("video_files", []) or []
        # dikey + makul cozunurluk (>=720 gen) tercih; en yakin dosyayi sec
        best, best_h = None, 0
        for f in files:
            h = f.get("height") or 0
            w = f.get("width") or 0
            if h >= w and 720 <= h <= 1920 and h > best_h:
                best, best_h = f, h
        if not best and files:
            best = files[0]
        if best and best.get("link"):
            out.append({"url": best["link"], "source": "pexels",
                        "license": "Pexels", "title": v.get("url", "")})
    return out


def pixabay_video_search(query, limit=6):
    """Pixabay Video (PIXABAY_API_KEY)."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://pixabay.com/api/videos/", params={
            "key": key, "q": query, "per_page": limit, "safesearch": "true",
        }, headers=HEADERS, timeout=25)
        hits = r.json().get("hits", []) or []
    except Exception:
        return []
    out = []
    for h in hits:
        vids = h.get("videos", {}) or {}
        pick = vids.get("large") or vids.get("medium") or vids.get("small") or {}
        if pick.get("url"):
            out.append({"url": pick["url"], "source": "pixabay",
                        "license": "Pixabay", "title": h.get("tags", "")})
    return out


VIDEO_CONNECTORS = [pexels_video_search, pixabay_video_search]


def _clip_candidates(term):
    cands, seen = [], set()
    for connector in VIDEO_CONNECTORS:
        try:
            for it in connector(term):
                if it["url"] not in seen:
                    seen.add(it["url"])
                    cands.append(it)
        except Exception:
            continue
    return cands


def _download_clip(candidates, dest, skip_urls=None):
    """skip_urls: onceki beat'lerde kullanilmis klipler (tekrar onleme, once
    taze adaylar denenir; baska aday yoksa tekrara izin verilir)."""
    if skip_urls:
        fresh = [c for c in candidates if c.get("url") not in skip_urls]
        meta = _download_clip(fresh, dest) if fresh else None
        if meta:
            return meta
    for cand in candidates:
        try:
            resp = requests.get(cand["url"], headers=HEADERS, timeout=120, stream=True)
            if resp.status_code != 200:
                continue
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            if os.path.getsize(dest) < 20000:
                continue
            return {"source": cand["source"], "license": cand["license"],
                    "source_url": cand["url"], "title": cand.get("title", ""),
                    "relevance": round(cand.get("_rel", 0), 1)}
        except Exception:
            continue
    return None


def _fetch_clips_per_query(queries, kinds=None):
    """BEAT-SENKRON video: her query (beat) icin O ana en alakali TEK klibi
    indirir; sonuc queries ile 1:1 hizali. Bos beat komsudan dongulenir.

    NOT: klip kaynaklari (Pixabay/Pexels) yalnizca STOK oldugu icin capa sarti
    uygulanamaz -- spesifik bir olayin gercek videosu stokta yoktur. kinds
    yalnizca beat raporuna yazilir: bir konuda cok sayida 'specific' beat
    varsa o konu photo modunda uretilmelidir."""
    clips, used, src_counts = [None] * len(queries), [None] * len(queries), {}
    seen_urls = set()           # ayni klip birden fazla beat'e gitmesin
    for i, q in enumerate(queries):
        dest = os.path.join(ASSETS_DIR, f"clip_{i}.mp4")
        # ALAKA-ONCELIKLI: klip kaynaklari (Pixabay/Pexels) yalniz stok oldugu icin
        # eleme yapilamaz (havuz bosalir), ama en cok tutan aday one alinir.
        cands = _clip_candidates(q)
        for c in cands:
            c["_rel"] = _relevance((c.get("title") or "").lower(), q, stock=True)[0]
        cands.sort(key=lambda c: c["_rel"], reverse=True)
        meta = _download_clip(cands, dest, skip_urls=seen_urls)
        if meta:
            clips[i] = dest
            used[i] = dict(meta, kind=(kinds[i] if kinds and i < len(kinds) else "specific"))
            seen_urls.add(meta["source_url"])
            src_counts[meta["source"]] = src_counts.get(meta["source"], 0) + 1
    if any(c is None for c in clips) and any(clips):
        n = len(clips)
        for i in range(n):
            if clips[i]:
                continue
            j = next((k for d in range(1, n) for k in (i - d, i + d)
                      if 0 <= k < n and clips[k]), None)
            if j is not None:
                clips[i] = clips[j]
                used[i] = dict(used[j] or {}, reused=True)
                src_counts["reused"] = src_counts.get("reused", 0) + 1
    return clips, used, src_counts


@app.route('/fetch_clips', methods=['POST'])
def fetch_clips():
    """Sahne basina GERCEK video klip indir (telifsiz). fetch_scenes'in video
    karsiligi. queries (somut arama terimleri) onerilir; yoksa subject.
    Doner: clip_0.mp4.. yollari + kaynak dokumu."""
    data = request.get_json(silent=True) or {}
    subject = data.get('subject')
    scene_count = data.get('scene_count')
    queries = data.get('queries') or data.get('image_query')
    if isinstance(queries, str):
        queries = [queries]
    per_query = bool(data.get('per_query'))
    terms = [q for q in (queries or []) if q and q.strip()] or [subject]

    if not subject:
        return jsonify({"error": "subject gerekli"}), 400

    if not (os.environ.get("PEXELS_API_KEY") or os.environ.get("PIXABAY_API_KEY")):
        return jsonify({
            "error": "video kaynagi icin PEXELS_API_KEY veya PIXABAY_API_KEY gerekli",
            "how": "pexels.com/api ve pixabay.com/api/docs -> ucretsiz key -> ortam degiskenine ekle",
        }), 400

    # BEAT-SENKRON YOL: her query'ye 1:1 hizali klip
    if per_query and queries:
        for p in glob.glob(os.path.join(ASSETS_DIR, "clip_*.mp4")):
            os.remove(p)
        clips, used, src_counts = _fetch_clips_per_query(queries, kinds=data.get('kinds'))
        if not any(clips):
            return jsonify({"error": f"'{subject}' icin hicbir beat'e klip bulunamadi",
                            "per_query": True}), 404
        print(f"[fetch_clips:per_query] subject={subject!r} beats={len(queries)} "
              f"kaynak={src_counts}")
        return jsonify({"success": True, "subject": subject, "per_query": True,
                        "scene_count": len(clips), "clips": clips,
                        "sources": used, "source_breakdown": src_counts})
    try:
        scene_count = max(1, int(scene_count))
    except (TypeError, ValueError):
        return jsonify({"error": "scene_count sayi olmali"}), 400

    # aday klipleri topla (cesitlilik icin terimler arasi donusumlu)
    candidates, seen = [], set()
    for term in terms:
        for connector in VIDEO_CONNECTORS:
            try:
                for it in connector(term):
                    if it["url"] not in seen:
                        seen.add(it["url"])
                        candidates.append(it)
            except Exception:
                continue

    if not candidates:
        return jsonify({"error": f"'{subject}' icin klip bulunamadi"}), 404

    clips, used, src_counts, idx = [], [], {}, 0
    for cand in candidates:
        if len(clips) >= scene_count:
            break
        try:
            resp = requests.get(cand["url"], headers=HEADERS, timeout=120, stream=True)
            if resp.status_code != 200:
                continue
            dest = os.path.join(ASSETS_DIR, f"clip_{idx}.mp4")
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            if os.path.getsize(dest) < 20000:
                continue
            clips.append(dest)
            src = cand["source"]
            src_counts[src] = src_counts.get(src, 0) + 1
            used.append({"source": src, "license": cand["license"], "source_url": cand["url"]})
            idx += 1
        except Exception:
            continue

    # yetmezse dongu-tekrar (alakasiz dolgu yok)
    reused = 0
    if 0 < len(clips) < scene_count:
        base = list(clips)
        i = 0
        while len(clips) < scene_count:
            clips.append(base[i % len(base)])
            i += 1
            reused += 1
        src_counts["reused"] = reused

    if not clips:
        return jsonify({"error": "klipler indirilemedi"}), 500

    print(f"[fetch_clips] subject={subject!r} kaynak={src_counts}")
    return jsonify({"success": True, "subject": subject, "scene_count": len(clips),
                    "clips": clips, "sources": used, "source_breakdown": src_counts})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5080)
