"""
CLIP tabanli gorsel dogrulama sunucusu (port 5004).
Aday gorselleri beat'in "visual" tarifiyle karsilastirir ve siralar.
Esik degil, SIRALAYICI olarak calisir.

POST /rank  → adaylar icinde en iyisini sec
GET /health → durum bilgisi
"""
from flask import Flask, request, jsonify
import os, sys, torch, tempfile
from PIL import Image

app = Flask(__name__)

# ── Model yukleme (bir kez, baslangicta) ──────────────────────────────
MODEL_ID = "openai/clip-vit-base-patch32"
pipe = None
_available = False
_device = "cpu"

def _load_model():
    global pipe, _available, _device
    try:
        from transformers import CLIPProcessor, CLIPModel
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"CLIP yukleniyor ({_device})...")
        model = CLIPModel.from_pretrained(MODEL_ID).to(_device)
        processor = CLIPProcessor.from_pretrained(MODEL_ID)
        pipe = {"model": model, "processor": processor, "device": _device}
        _available = True
        print(f"CLIP hazir ({_device})")
    except Exception as e:
        print(f"CLIP yuklenemedi: {e}", file=sys.stderr)
        _available = False
        pipe = None

_load_model()

# ── Endpoint'ler ──────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "model": MODEL_ID if _available else None,
        "device": _device,
        "available": _available,
    })


@app.route("/rank", methods=["POST"])
def rank():
    """Aday gorselleri bir visual tarif ile sirala.

    Input (JSON):
        text  : beat'in kisa Ingilizce gorsel tarifi (ozel isim icermemeli)
        images: dosya yollari listesi (en az 1, en fazla 10)

    Input (multipart form - photo_server uyumluluğu):
        query : beat'in kisa Ingilizce gorsel tarifi
        images: dosya yuklemeleri (file uploads)

    Output:
        ranked      : [{index, score}] sirali liste
        scores      : her aday icin kosinüs benzerlikleri
        best_index  : en yuksek puana sahip adayin indeksi
        separation  : en iyi - ortalama (ayrisma miktari)
        confident   : ayrisma yeterli mi (medyan > 0.02)
    """
    if not _available:
        return jsonify({"error": "CLIP modeli kullanilamiyor", "scores": [],
                        "best_index": -1, "separation": 0, "confident": False}), 503

    # Hem JSON hem multipart form data destegi
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    images = data.get("images", [])

    # Multipart form data (photo_server tarafindan gonderilen)
    if not text:
        text = request.form.get("query", "").strip()
    if not images and request.files:
        images = []
        for key in sorted(request.files.keys()):
            if key == "images":
                f = request.files[key]
                import tempfile
                fd, tmp_path = tempfile.mkstemp(suffix=".png")
                with os.fdopen(fd, "wb") as out:
                    out.write(f.read())
                images.append(tmp_path)

    if not text:
        return jsonify({"error": "text/query gerekli"}), 400
    if not images:
        return jsonify({"error": "images bos liste olamaz"}), 400

    # Gorselleri yukle
    pil_images = []
    valid_indices = []
    temp_files = []
    for i, img_path in enumerate(images):
        try:
            pil_images.append(Image.open(img_path).convert("RGB"))
            valid_indices.append(i)
            # Gecici dosyalari isaretleyelim (sonra temizleyecegiz)
            if img_path.startswith(tempfile.gettempdir()) or "_clip_cand_" in img_path:
                temp_files.append(img_path)
        except Exception as e:
            print(f"Gorsel yuklenemedi ({img_path}): {e}", file=sys.stderr)

    if not pil_images:
        return jsonify({"error": "hicbir gorsel yuklenemedi", "scores": [],
                        "best_index": -1, "separation": 0, "confident": False}), 500

    # CLIP cikarimi
    model = pipe["model"]
    processor = pipe["processor"]
    device = pipe["device"]

    inputs = processor(
        text=[text] * len(pil_images),
        images=pil_images,
        return_tensors="pt",
        padding=True,
    ).to(device)

    with torch.no_grad():
        outputs = model(**inputs)

    # Kosinüs benzerligi (logits_per_image)
    logits = outputs.logits_per_image  # shape: (N, 1)
    scores_raw = logits.squeeze(dim=1).tolist()

    # Ham skorlari orijinal indekslere esle
    all_scores = [0.0] * len(images)
    for j, idx in enumerate(valid_indices):
        all_scores[idx] = scores_raw[j]

    # Siralama
    best_idx = max(range(len(all_scores)), key=lambda k: all_scores[k])
    mean_score = sum(all_scores) / len(all_scores) if all_scores else 0
    separation = all_scores[best_idx] - mean_score

    # Medyan uzerinden guven esigi
    sorted_scores = sorted(all_scores)
    median = sorted_scores[len(sorted_scores) // 2]
    confident = separation > 0.02  # en iyi aday digerlerinden belirgin sekilde yuksek mi?

    # Ranked listesi (photo_server'in bekledigi format)
    ranked = [{"index": i, "score": round(all_scores[i], 4)} for i in range(len(all_scores))]
    ranked.sort(key=lambda x: x["score"], reverse=True)

    # Gecici dosyalari temizle
    for tf in temp_files:
        try:
            if os.path.exists(tf):
                os.remove(tf)
        except:
            pass

    return jsonify({
        "ranked": ranked,
        "scores": [round(s, 4) for s in all_scores],
        "best_index": best_idx,
        "separation": round(separation, 4),
        "confident": confident,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004)
