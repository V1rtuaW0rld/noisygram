FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake au build : le runtime ne touche JAMAIS le réseau.
# (pas de curl dans l'image → urllib)
RUN python - <<'PY'
import hashlib, pathlib, urllib.request
ASSETS = [
    ("https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite",
     "yamnet.tflite",
     "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf"),
    ("https://raw.githubusercontent.com/tensorflow/models/"
     "dfffd623b6be8d1d9744b8e261fbac370d17c46d/research/audioset/yamnet/yamnet_class_map.csv",
     "yamnet_class_map.csv",
     "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"),
    # Chart.js vendu au build : le vieux PC derrière une box grand public est
    # exactement la machine la plus susceptible de ne pas avoir de DNS.
    ("https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js",
     "chart.umd.min.js", None),
]
models = pathlib.Path("/app/models"); models.mkdir(parents=True, exist_ok=True)
vendor = pathlib.Path("/app/static/vendor"); vendor.mkdir(parents=True, exist_ok=True)
for url, name, want in ASSETS:
    req = urllib.request.Request(url, headers={"User-Agent": "noisygram-build"})
    data = urllib.request.urlopen(req, timeout=180).read()
    if want is not None:
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise SystemExit(f"checksum mismatch for {name}: got {got}, want {want}")
    out = vendor if name.endswith(".js") else models
    (out / name).write_bytes(data)
    print(f"ok {name} {len(data)} bytes")
PY

COPY app    ./app
COPY static ./static
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
