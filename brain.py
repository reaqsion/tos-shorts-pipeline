"""
TOS Beyni v1: bir NIS ya da KONU verilir; LLM (Ollama, lokal) hook'lu senaryo +
gorsel sorgulari + foto/video karari uretir. Cikti dogrudan render pipeline'ina
beslenebilen JSON.

Model degistirilebilir: env BRAIN_MODEL (varsayilan qwen2.5:7b).
Cloud'a gecmek istenirse sadece _call_llm degisir.
"""
import requests
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import json
import os
import time
import socket
import subprocess

_GAPI_HOST = "generativelanguage.googleapis.com"
_ip_cache = None  # bu makinenin DNS'i araliklidir; iyi bir cozumu onbellekle


def _resolve_host(host, tries=10):
    """host'u getaddrinfo ile cozer; araliklidir diye retry'li + onbellekli.
    OS resolver inatci olursa bir kez 'ipconfig /flushdns' dener."""
    global _ip_cache
    flushed = False
    for i in range(tries):
        try:
            ip = socket.gethostbyname(host)
            _ip_cache = ip
            return ip
        except socket.gaierror:
            if not flushed:
                try:
                    subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10)
                except Exception:
                    pass
                flushed = True
            time.sleep(min(2 * (i + 1), 12))
    if _ip_cache:               # son care: daha once cozulmus IP
        return _ip_cache
    raise socket.gaierror(f"{host} cozulemedi ({tries} deneme)")


def _gemini_post(model, body, timeout=120, tries=6):
    """Gemini generateContent POST'u; bu makinenin araliklı DNS'ine karsi dayanikli.
    Her denemede once host cozulur (onbellek/flush/retry ile), sonra POST edilir.
    Tek bir getaddrinfo hatasi tum run'i oldurmesin."""
    key = os.environ["GEMINI_API_KEY"]
    url = (f"https://{_GAPI_HOST}/v1beta/models/{model}:generateContent?key={key}")
    last = None
    for i in range(tries):
        try:
            _resolve_host(_GAPI_HOST)   # DNS penceresi acilana dek bekler
            r = requests.post(url, json=body, timeout=timeout)
            # Gecici sunucu hatalarinda (429 kota/500/503 mesgul) retry et
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.exceptions.HTTPError(f"{r.status_code}", response=r)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                socket.gaierror, requests.exceptions.HTTPError) as e:
            # Kalici 4xx (400/401/403/404) retry'lanamaz -> hemen firlat
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code and code not in (429, 500, 502, 503, 504):
                raise
            last = e
            wait = min(3 * (i + 1), 20)
            print(f"  [gemini] gecici hata ({i+1}/{tries}), {wait}s bekle: "
                  f"{type(e).__name__} {code or ''}")
            time.sleep(wait)
    raise last

def _gemini_any(body, timeout=120, tries=3):
    """Once varsayilan model, mesgulse (503/429) sirayla yedek modeller.
    Tek bir modelin yogunlugu tum uretimi durdurmasin."""
    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
    last = None
    for m in models:
        try:
            return _gemini_post(m, body, timeout=timeout, tries=tries)
        except Exception as e:
            last = e
            code = getattr(getattr(e, "response", None), "status_code", None)
            print(f"  [gemini] {m} kullanilamadi ({code or type(e).__name__}), "
                  f"yedek modele geciliyor")
    raise last


OLLAMA = "http://localhost:11434/api/chat"
MODEL = os.environ.get("BRAIN_MODEL", "qwen2.5:7b")
# Backend secimi: GEMINI_API_KEY varsa Gemini (kaliteli), yoksa yerel Ollama.
BACKEND = os.environ.get("BRAIN_BACKEND", "gemini" if os.environ.get("GEMINI_API_KEY") else "ollama")
# NOT: "gemini-2.5-flash" direkt alias'i Google tarafindan YENI hesaplara kapatildi
# (404 "no longer available to new users"). 2026-08-17: "gemini-flash-latest" saatlerce
# 503 ("high demand") verdi ve uretimi tamamen durdurdu -> varsayilan calisan surume
# alindi, ayrica model DUSME ZINCIRI eklendi (biri mesgulse digerine gecer).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACKS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACKS",
    "gemini-flash-latest,gemini-3.1-flash-lite,gemini-3-flash-preview").split(",")
    if m.strip()]

_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MASTER_PROMPT.md")
if os.path.exists(_PROMPT_FILE):
    # Tek kaynak: MASTER_PROMPT.md (Gemini'ye elle verdigin prompt'un AYNISI)
    SYSTEM = open(_PROMPT_FILE, encoding="utf-8").read()
else:
    SYSTEM = """Sen, milyonlarca izlenen Türkçe YouTube Shorts kanallarının baş senaristisin.
Görevin: verilen konu için KUSURSUZ, akıcı, doğal Türkçe bir kısa video senaryosu yazmak.

MUTLAK KURALLAR:
1. DİL: Kusursuz, doğal, anadili Türkçe. Devrik/bozuk cümle YOK. Uydurma kelime YOK.
   Çeviri kokan ifade YOK. Bir Türk gibi yaz.
2. HOOK (ilk cümle): İlk 2 saniyede merak boşluğu aç. Soru ya da şok bir iddia.
   Betimleme değil, İDDİA. Örn: "Bu adam bir sayıyı yanlış seçti ve 77 milyon kişi hacklendi."
3. CÜMLELER: Kısa ve vurucu. Her cümle TEK fikir. Şimdiki/geniş zaman, aktif fiil.
   Dolgu kelime yok. Seslendirme için akıcı olmalı.
4. UZUNLUK: 6-9 cümle (~30-40 saniye seslendirme).
5. KAPANIŞ: Merakı kapatan ya da düşündüren tek cümle.
6. GÖRSEL SORGULARI (queries): 6-8 adet İNGİLİZCE somut arama terimi. Soyut kavram
   DEĞİL, onu temsil eden somut kişi/eser/obje/sahne. Telifsiz görsel bulunabilecek
   şeyler iste (gerçek kişiler, tarihi görseller, jenerik atmosfer).
7. GÖRSEL MODU (visual_mode): konu MODERN/teknoloji/doğa ise "video"; DÖNEM/tarihsel/
   sanat ise "photo" yaz.

SADECE şu JSON formatında yanıt ver, başka hiçbir şey yazma:
{"topic":"...","hook":"...","narration":"...","queries":["...","..."],"visual_mode":"video|photo"}
"""

# few-shot: kaliteyi + BEAT-gorsel uyumunu gostermek icin ALTIN ornek (PS3)
EXAMPLE_USER = "Konu: PlayStation 3 hack olayı (2011 Sony PSN ihlali)"
EXAMPLE_ASSISTANT = json.dumps({
    "topic": "2011 Sony PlayStation Network hack'i",
    "anchor": "PlayStation 3 Sony",
    "hook_overlay": "77 milyon hesap nasıl çalındı?",
    "beats": [
        {"text": "Kriptografi her imza için rastgele bir sayı ister.", "query": "cryptography code encryption screen", "visual": "computer screen showing code and encryption symbols", "kind": "generic"},
        {"text": "Ama Sony, PlayStation 3'te hep aynı sabit sayıyı kullandı.", "query": "PlayStation 3 console", "visual": "a white gaming console on a shelf", "kind": "specific"},
        {"text": "Bu hata konsolun gizli ana anahtarını herkese açtı.", "query": "digital security key concept", "visual": "a glowing digital key on dark background", "kind": "generic"},
        {"text": "George Hotz bu anahtarı buldu.", "query": "George Hotz portrait", "visual": "a young man with short hair looking at camera", "kind": "specific"},
        {"text": "Ve internete koydu.", "query": "code on computer forum", "visual": "text on a computer screen forum page", "kind": "generic"},
        {"text": "Sony panikledi, onu mahkemeye verdi.", "query": "courtroom gavel law", "visual": "a wooden gavel on judge desk", "kind": "generic"},
        {"text": "Ama bu hamle tüm hacker dünyasını kızdırdı.", "query": "hacker in hoodie dark room", "visual": "person in dark hoodie at computer", "kind": "generic"},
        {"text": "Nisan 2011'de birileri ağa sızdı.", "query": "PlayStation Network data center", "visual": "server room with blinking lights", "kind": "specific"},
        {"text": "77 milyon hesap çalındı.", "query": "stolen data leak concept", "visual": "binary data flowing on screen", "kind": "generic"},
        {"text": "Sony ağı yirmi üç gün kapatmak zorunda kaldı.", "query": "network offline error screen", "visual": "red error message on computer screen", "kind": "generic"},
        {"text": "Saldırganın kim olduğu bugüne dek bulunamadı.", "query": "Anonymous Guy Fawkes mask", "visual": "a white mask with mustache and beard", "kind": "specific"},
    ],
    "visual_mode": "video",
    "youtube_title": "Sony'nin 77 Milyon Kişiyi Hackleten Küçük Hatası",
    "youtube_description": ("Tarihin en büyük veri sızıntılarından biri tek bir matematik "
                            "hatasıyla başladı. #playstation #hacker #sibergüvenlik #teknoloji #tarih"),
}, ensure_ascii=False)


def _call_gemini(topic, style=None):
    system = SYSTEM
    if style:
        # Nise ozel ton/stil: kanal bazli (channels.json'daki "style" alani).
        system = SYSTEM + "\n\n## BU KANALIN STİLİ (harfiyen uy)\n" + style
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "user", "parts": [{"text": EXAMPLE_USER}]},
            {"role": "model", "parts": [{"text": EXAMPLE_ASSISTANT}]},
            {"role": "user", "parts": [{"text": f"Konu: {topic}"}]},
        ],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    return _gemini_any(body)


def _call_ollama(topic, style=None):
    system = SYSTEM + ("\n\n## BU KANALIN STİLİ\n" + style if style else "")
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": EXAMPLE_USER},
            {"role": "assistant", "content": EXAMPLE_ASSISTANT},
            {"role": "user", "content": f"Konu: {topic}"},
        ],
        "stream": False, "format": "json", "options": {"temperature": 0.7},
    }
    r = requests.post(OLLAMA, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"]


def _call_llm(topic, style=None):
    return _call_gemini(topic, style) if BACKEND == "gemini" else _call_ollama(topic, style)


def _repair_json(raw):
    """Metin ICINDE kacissiz tirnak birakan LLM ciktisini onarir.

    Gercek vaka: model "Fransiz gemisi "Mont-Blanc" limana girdi" yaziyor ve
    JSON kiriliyor ("Expecting ',' delimiter"). Bir string'in icindeki tirnak,
    ancak ardindan (bosluklar atlanarak) , : } ] gelmiyorsa KAPATMA tirnagi
    degildir -> kacisla degistirilir."""
    out, in_str, esc = [], False, False
    for i, ch in enumerate(raw):
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if in_str and ch in "\n\r\t":
            # JSON string'i icinde ham satir sonu/tab yasak -> kacisla degistir
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            nxt = next((c for c in raw[i + 1:] if not c.isspace()), "")
            if nxt in (",", ":", "}", "]", ""):
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')          # string icinde kalmis tirnak
            continue
        out.append(ch)
    return "".join(out)


def _loads_first(raw):
    """LLM bazen gecerli JSON'dan sonra fazladan metin/ikinci obje dondurur
    ('Extra data'), bazen de string icinde kacissiz tirnak birakir. Ikisine de
    toleransli olarak ilk JSON objesini ayikla."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start == -1:
        raise json.JSONDecodeError("JSON objesi yok", raw, 0)
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        return obj
    except json.JSONDecodeError:
        # son care: kacissiz tirnaklari onar, tekrar dene
        obj, _ = json.JSONDecoder().raw_decode(_repair_json(raw[start:]))
        return obj


def generate_scenario(topic, style=None, _kalan=2):
    raw = _call_llm(topic, style)
    try:
        data = _loads_first(raw)
    except (json.JSONDecodeError, ValueError):
        # LLM bazen onarilamayacak kadar bozuk JSON dondurur -> yeniden uret.
        if _kalan <= 0:
            raise
        print(f"  [beyin] bozuk JSON geldi, yeniden deneniyor ({_kalan} hak kaldi)")
        return generate_scenario(topic, style, _kalan - 1)
    if data.get("rejected"):          # konu filtresi (telifli) -> aynen gecir
        return data
    # minimal dogrulama (hook_overlay ya da hook kabul)
    if not data.get("hook_overlay") and not data.get("hook"):
        raise ValueError(f"hook yok\nham: {raw}")

    # BEAT MIMARISI: beats varsa narration + queries'i ondan TURET (senkron icin
    # produce.py beat zamanlamasini kullanir). Eski format (narration+queries)
    # da desteklenir (geriye donuk uyum).
    beats = data.get("beats")
    if isinstance(beats, list) and beats:
        clean = [b for b in beats if isinstance(b, dict) and b.get("text")]
        if not clean:
            raise ValueError(f"beats bos/gecersiz\nham: {raw}")
        data["beats"] = clean
        data["narration"] = " ".join(b["text"].strip() for b in clean)
        data["queries"] = [(b.get("query") or topic).strip() for b in clean]
        # Beat turu: "specific" (gercek arsiv gorseli sart) / "generic" (atmosfer
        # yeterli). Model soylemediyse guvenli taraf = specific.
        data["kinds"] = ["generic" if str(b.get("kind", "")).lower().startswith("gen")
                         else "specific" for b in clean]
        # CLIP icin gorsel tarifleri (ozel isimsiz, kisa, somut).
        # Alan yoksa query'ye dus (geriye donuk uyum).
        data["visuals"] = [(b.get("visual") or b.get("query") or topic).strip() for b in clean]
    else:
        for k in ("narration", "queries"):
            if not data.get(k):
                raise ValueError(f"eksik alan: {k} (ve beats yok)\nham: {raw}")

    data.setdefault("visual_mode", "photo")
    data.setdefault("topic", topic)
    return data


TOPIC_SYSTEM = """Sen bir Türkçe YouTube Shorts kanalı için KONU üreticisisin.
Verilen NİŞ için, izlenme potansiyeli yüksek, merak uyandıran, SOMUT konular üret.

KURALLAR:
- Her konu SERBEST görselle (telifsiz foto/video/arşiv) anlatılabilir olmalı.
- Telifli DİZİ/FİLM/kurgu karakter/güncel ünlü gerektiren konu ÜRETME.
- Gerçek olaylar, tarihi kişiler, bilimsel gerçekler, ilginç hikayeler seç.
- Her konu tek satır: spesifik ve çekici (klişe değil).

SADECE şu JSON'u döndür: {"topics": ["konu 1", "konu 2", ...]}"""


def generate_topics(niche, count=10):
    """Bir nis icin izlenmesi yuksek, serbest-gorselli SOMUT konular uret."""
    user = f"Niş: {niche}\n{count} adet konu üret."
    if BACKEND == "gemini":
        body = {
            "system_instruction": {"parts": [{"text": TOPIC_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
        }
        raw = _gemini_any(body)
    else:
        payload = {"model": MODEL, "messages": [
            {"role": "system", "content": TOPIC_SYSTEM},
            {"role": "user", "content": user}],
            "stream": False, "format": "json", "options": {"temperature": 0.9}}
        r = requests.post(OLLAMA, json=payload, timeout=180)
        r.raise_for_status()
        raw = r.json()["message"]["content"]
    return _loads_first(raw).get("topics", [])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--topics":
        niche = " ".join(sys.argv[2:]) or "Siber güvenlik ve hacker olayları"
        for t in generate_topics(niche, 10):
            print("-", t)
        sys.exit(0)
    topic = " ".join(sys.argv[1:]) or "Stuxnet: bir ülkenin nükleer santralini vuran ilk siber silah"
    print(f"MODEL: {MODEL}\nKONU: {topic}\n")
    s = generate_scenario(topic)
    print("HOOK:", s["hook"])
    print("\nNARRATION:", s["narration"])
    print("\nQUERIES:", s["queries"])
    print("VISUAL_MODE:", s["visual_mode"])
