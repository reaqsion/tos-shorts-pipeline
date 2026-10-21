"""
Edge-TTS sunucusu (Microsoft Azure neural sesleri, ucretsiz, key yok).
Dogal Turkce, Ingilizce sizmasi yok. Piper ile AYNI kontrat:
POST / {"text": "..."} -> 16-bit WAV byte'lari. n8n'de sadece URL degisir.

Online (Microsoft Edge servisi) ama ucretsiz. GPU kullanmaz, hizli.
Ses/dil HARDCODE degil: VOICES config'inden secilir (yeni kanal = yeni satir).
"""
from flask import Flask, request, Response, jsonify
import edge_tts
import asyncio
import subprocess
import tempfile
import os

app = Flask(__name__)

VOICES = {
    "tr_male": "tr-TR-AhmetNeural",     # varsayilan: erkek, belgesel tonu
    "tr_female": "tr-TR-EmelNeural",    # kadin
    "en_male": "en-US-GuyNeural",
    "en_female": "en-US-JennyNeural",
}
DEFAULT_VOICE = "tr_male"

# Istenen ses hic ses vermezse sirayla bunlar denenir (ayni dil once).
VOICE_FALLBACKS = ["tr-TR-EmelNeural", "tr-TR-AhmetNeural"]


def to_wav16(mp3_path, wav_path, trim_silence=True):
    # Pipeline formati: 24kHz mono 16-bit PCM (subtitle_server bunu okuyabiliyor).
    #
    # + CUMLE ARASI UZUN SESSIZLIKLERI KIRP: Edge-TTS cumle sonlarina cok uzun
    # bosluk koyuyor -> video "olu" hissi veriyor. silenceremove ile >=0.45sn
    # bosluklari ~0.30sn'e indiriyoruz (tamamen silmiyoruz; dogal ritim + nefes
    # kaliyor). Kelime ici duraklar (<0.45sn) ve konusma (>-38dB) DOKUNULMAZ.
    # ONEMLI: bu adim altyazi hizalamasindan ONCE (burada) yapilir; boylece
    # forced alignment kisalmis sese gore hizalanir, senkron BOZULMAZ.
    af = ("silenceremove="
          "start_periods=1:start_duration=0:start_threshold=-38dB:"
          "stop_periods=-1:stop_duration=0.45:stop_threshold=-38dB:stop_silence=0.30")
    # SIIR/DUYGU ISTISNASI: trim_silence=False ise bosluklara DOKUNULMAZ.
    # Siirde durak anlamin parcasidir; kirpmak metni acele ettirir.
    cmd = ["ffmpeg", "-y", "-i", mp3_path]
    if trim_silence:
        cmd += ["-af", af]
    cmd += ["-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", wav_path]
    subprocess.run(cmd, capture_output=True)


@app.route('/', methods=['POST'])
def synthesize():
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not text:
        return {"error": "text parametresi gerekli"}, 400

    voice_key = data.get('voice', DEFAULT_VOICE)
    voice = VOICES.get(voice_key, VOICES[DEFAULT_VOICE])
    rate = data.get('rate')    # ornek: "-5%" (yavaslat), "+10%" (hizlandir)
    pitch = data.get('pitch')  # ornek: "-2Hz"

    fd, mp3 = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)

    try:
        def dene(v):
            """Tek bir sesle sentez dener; uretilen mp3 boyutunu doner."""
            async def go():
                kwargs = {}
                if rate:
                    kwargs["rate"] = rate
                if pitch:
                    kwargs["pitch"] = pitch
                comm = edge_tts.Communicate(text, v, **kwargs)
                await comm.save(mp3)
            asyncio.run(go())
            return os.path.getsize(mp3) if os.path.exists(mp3) else 0

        # DAYANIKLILIK (2026-10-15): Edge-TTS bazi sesleri KARARSIZ donduruyor.
        # Olculdu: tr-TR-AhmetNeural 3 denemenin 1'inde ses veriyor, digerlerinde
        # "NoAudioReceived" (sunucu tarafi, kutuphane guncel). Butun kanallar bu
        # sesi kullandigi icin uretim tamamen duruyordu. Cozum: ayni sesi birkac
        # kez dene, yine olmazsa calisan yedek sese dus (uretim durmasin).
        kullanilan, boyut = voice, 0
        for v in [voice] + [x for x in VOICE_FALLBACKS if x != voice]:
            for _ in range(3):
                try:
                    boyut = dene(v)
                except Exception:
                    boyut = 0
                if boyut >= 500:
                    break
            if boyut >= 500:
                kullanilan = v
                break

        if boyut < 500:
            return {"error": "Edge-TTS hicbir sesle ses uretemedi",
                    "denenen": [voice] + VOICE_FALLBACKS}, 500
        if kullanilan != voice:
            print(f"  [tts] {voice} ses vermedi -> {kullanilan} ile uretildi")

        to_wav16(mp3, wav, trim_silence=bool(data.get("trim_silence", True)))
        with open(wav, "rb") as f:
            wav_bytes = f.read()
        return Response(wav_bytes, mimetype="audio/wav")
    except Exception as e:
        return {"error": "Edge-TTS hata verdi", "details": str(e)[-2000:]}, 500
    finally:
        for p in (mp3, wav):
            if os.path.exists(p):
                os.remove(p)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "voices": list(VOICES.keys()), "default": DEFAULT_VOICE})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002)
