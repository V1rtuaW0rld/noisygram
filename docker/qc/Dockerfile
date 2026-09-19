FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Modèle YAMNet embarqué au build
RUN python - <<'PY'
import hashlib, pathlib, urllib.request
url = "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite"
req = urllib.request.Request(url, headers={"User-Agent": "qc-build"})
data = urllib.request.urlopen(req, timeout=180).read()
want = "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf"
got = hashlib.sha256(data).hexdigest()
if got != want:
    raise SystemExit(f"checksum mismatch: got {got}, want {want}")
models = pathlib.Path("/app/models")
models.mkdir(parents=True, exist_ok=True)
(models / "yamnet.tflite").write_bytes(data)
print("ok yamnet.tflite")
PY

COPY qc_server.py .

EXPOSE 8001

HEALTHCHECK --interval=20s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["uvicorn", "qc_server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
