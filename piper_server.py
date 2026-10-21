from flask import Flask, request, Response
from dotenv import load_dotenv
load_dotenv()  # API anahtarlari .env'den
import subprocess
import tempfile
import os

app = Flask(__name__)

PIPER_DIR = os.environ.get("PIPER_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper"))
PIPER_EXE = os.path.join(PIPER_DIR, "piper.exe")

# Sesler: istekte "voice" gelmezse varsayilan Turkce (kanallar Turkce icerik).
# Ingilizce icin istekte {"voice": "en"} gonder.
VOICES = {
    "tr": os.path.join(PIPER_DIR, "tr_TR-dfki-medium.onnx"),
    "en": os.path.join(PIPER_DIR, "en_US-ryan-medium.onnx"),
}
DEFAULT_VOICE = "tr"


@app.route('/', methods=['POST'])
def synthesize():
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not text:
        return {"error": "text parametresi gerekli"}, 400

    voice = data.get('voice', DEFAULT_VOICE)
    model_path = VOICES.get(voice, VOICES[DEFAULT_VOICE])
    if not os.path.exists(model_path):
        return {"error": f"Ses modeli bulunamadi: {model_path}"}, 500

    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)

    cmd = [PIPER_EXE, "-m", model_path, "-f", out_path]
    result = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PIPER_DIR)

    if result.returncode != 0 or not os.path.exists(out_path):
        return {"error": "Piper hata verdi", "details": result.stderr[-2000:]}, 500

    with open(out_path, "rb") as f:
        wav_bytes = f.read()
    os.remove(out_path)

    return Response(wav_bytes, mimetype="audio/wav")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
