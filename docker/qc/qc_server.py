"""Microservice QC (Quality Control acoustique) pour Noisygram.

Architecture inspirée d'AudioBookStudio :
- Extraction d'empreintes vectorielles (1024-d YAMNet) par tranche de 0.96 s.
- Moyenne et normalisation L2 pour former l'empreinte globale du clip.
- Calcul du score de similarité cosinus contre l'empreinte de référence.
- Possibilité d'enrichir l'empreinte de référence à partir d'une sélection de WAVs.
"""

from __future__ import annotations

import logging
import os
import threading
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from ai_edge_litert.interpreter import Interpreter
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("noisygram-qc")

MODEL_PATH = os.environ.get("QC_MODEL_PATH", "/app/models/yamnet.tflite")
REFERENCE_DIR = Path(os.environ.get("REFERENCE_DIR", "/data/reference"))
ACTIVE_REF_PATH = REFERENCE_DIR / "reference.npy"
DEFAULT_REF_WAV = REFERENCE_DIR / "reference.wav"

WINDOW_SAMPLES = 15_600
HOP_SAMPLES = 7_800
EMBEDDING_TENSOR_INDEX = 115

_interpreter: Interpreter | None = None
_scale: float = 1.0
_zero_point: int = 0
_active_ref_embedding: np.ndarray | None = None
_active_ref_sample_count: int = 0


def _get_interpreter() -> Interpreter:
    global _interpreter, _scale, _zero_point
    if _interpreter is None:
        if not os.path.exists(MODEL_PATH):
            raise RuntimeError(f"Modèle YAMNet introuvable : {MODEL_PATH}")
        _interpreter = Interpreter(MODEL_PATH, experimental_preserve_all_tensors=True)
        _interpreter.allocate_tensors()
        details = _interpreter.get_tensor_details()[EMBEDDING_TENSOR_INDEX]
        scales = details.get("quantization_parameters", {}).get("scales", [])
        zps = details.get("quantization_parameters", {}).get("zero_points", [])
        _scale = float(scales[0]) if len(scales) > 0 else 1.0
        _zero_point = int(zps[0]) if len(zps) > 0 else 0
        log.info("Interpréteur LiteRT initialisé (tensor %d, scale=%f, zp=%d)", EMBEDDING_TENSOR_INDEX, _scale, _zero_point)
    return _interpreter


def read_audio_16k(path: str | Path) -> tuple[np.ndarray, float]:
    """Lit un fichier WAV et renvoie un tableau float32 mono à 16 kHz ainsi que sa durée."""
    path_str = str(path)
    if not os.path.exists(path_str):
        raise FileNotFoundError(f"Fichier introuvable : {path_str}")

    with wave.open(path_str, "rb") as w:
        sr = w.getframerate()
        n_frames = w.getnframes()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        raw = w.readframes(n_frames)

    duration_s = n_frames / float(sr) if sr > 0 else 0.0

    if sampwidth == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Format PCM non supporté : sampwidth={sampwidth}")

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    if sr != 16000 and len(audio) > 0:
        target_len = int(round(len(audio) * 16000 / sr))
        audio = np.interp(
            np.linspace(0, len(audio), target_len, endpoint=False),
            np.arange(len(audio)),
            audio,
        )

    return audio.astype(np.float32), duration_s


_interpreter_lock = threading.Lock()


def compute_embedding_from_audio(audio: np.ndarray) -> np.ndarray:
    """Découpe l'audio en fenêtres YAMNet et extrait l'embedding moyen normalisé L2."""
    n = len(audio)
    if n == 0:
        return np.zeros(1024, dtype=np.float32)

    if n < WINDOW_SAMPLES:
        padded = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        padded[:n] = audio
        windows = [padded]
    else:
        windows = [audio[s : s + WINDOW_SAMPLES] for s in range(0, n - WINDOW_SAMPLES + 1, HOP_SAMPLES)]

    embs: list[np.ndarray] = []
    with _interpreter_lock:
        interp = _get_interpreter()
        for w in windows:
            interp.set_tensor(0, w)
            interp.invoke()
            raw = interp.get_tensor(EMBEDDING_TENSOR_INDEX).reshape(-1).astype(np.float32).copy()
            dequant = (raw - _zero_point) * _scale
            norm = float(np.linalg.norm(dequant))
            if norm > 1e-8:
                dequant = dequant / norm
            embs.append(dequant)

    if not embs:
        return np.zeros(1024, dtype=np.float32)

    mean_emb = np.mean(embs, axis=0)
    norm = float(np.linalg.norm(mean_emb))
    return (mean_emb / norm).astype(np.float32) if norm > 1e-8 else mean_emb


def compute_embedding_from_wav(path: str | Path) -> tuple[np.ndarray, float]:
    audio, duration_s = read_audio_16k(path)
    return compute_embedding_from_audio(audio), duration_s


def load_or_init_reference() -> None:
    global _active_ref_embedding, _active_ref_sample_count
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    if ACTIVE_REF_PATH.exists():
        try:
            _active_ref_embedding = np.load(str(ACTIVE_REF_PATH)).astype(np.float32)
            _active_ref_sample_count = 1
            log.info("Empreinte de référence chargée depuis %s", ACTIVE_REF_PATH)
            return
        except Exception as exc:
            log.warning("Impossible de lire %s : %r, tentative de régénération", ACTIVE_REF_PATH, exc)

    if DEFAULT_REF_WAV.exists():
        try:
            log.info("Génération de l'empreinte de référence depuis %s", DEFAULT_REF_WAV)
            emb, _ = compute_embedding_from_wav(DEFAULT_REF_WAV)
            np.save(str(ACTIVE_REF_PATH), emb)
            _active_ref_embedding = emb
            _active_ref_sample_count = 1
            log.info("Empreinte générée et sauvée dans %s", ACTIVE_REF_PATH)
            return
        except Exception as exc:
            log.exception("Erreur lors de la génération depuis %s : %r", DEFAULT_REF_WAV, exc)

    log.warning("Aucune référence audio disponible dans %s", REFERENCE_DIR)
    _active_ref_embedding = None
    _active_ref_sample_count = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    _get_interpreter()
    load_or_init_reference()
    yield


app = FastAPI(title="Noisygram Quality Center (QC) Service", lifespan=lifespan)


class VerifyRequest(BaseModel):
    wav_path: str
    ref_npy: str | None = None


class VerifyResponse(BaseModel):
    score: float
    percentage: float
    duration_s: float


class SoundprintRequest(BaseModel):
    wav_path: str
    output_npy: str


class BuildReferenceRequest(BaseModel):
    wav_paths: list[str]
    output_npy: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_loaded": _interpreter is not None,
        "reference_loaded": _active_ref_embedding is not None,
        "reference_path": str(ACTIVE_REF_PATH) if ACTIVE_REF_PATH.exists() else None,
        "reference_samples_count": _active_ref_sample_count,
    }


@app.post("/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest) -> VerifyResponse:
    """Calcule la similarité cosinus entre un fichier WAV et l'empreinte de référence."""
    if not os.path.exists(req.wav_path):
        raise HTTPException(status_code=400, detail=f"Fichier audio introuvable : {req.wav_path}")

    ref_vec = _active_ref_embedding
    if req.ref_npy:
        if not os.path.exists(req.ref_npy):
            raise HTTPException(status_code=400, detail=f"Fichier référence introuvable : {req.ref_npy}")
        ref_vec = np.load(req.ref_npy).astype(np.float32)

    if ref_vec is None:
        raise HTTPException(status_code=503, detail="Aucune empreinte de référence active chargée")

    try:
        test_vec, duration_s = compute_embedding_from_wav(req.wav_path)
        similarity = float(np.dot(ref_vec, test_vec))
        # Normalisation dans [0.0, 1.0] pour éviter d'éventuelles légères dérives d'arrondi
        similarity = max(0.0, min(1.0, similarity))
        percentage = round(similarity * 100.0, 1)
        return VerifyResponse(score=similarity, percentage=percentage, duration_s=round(duration_s, 2))
    except Exception as exc:
        log.exception("Erreur lors de la vérification de %s : %r", req.wav_path, exc)
        raise HTTPException(status_code=500, detail=f"Erreur d'analyse QC : {exc}") from exc


@app.post("/soundprint")
def create_soundprint(req: SoundprintRequest) -> dict[str, str]:
    """Crée une empreinte .npy pour un fichier WAV donné."""
    if not os.path.exists(req.wav_path):
        raise HTTPException(status_code=400, detail=f"Fichier audio introuvable : {req.wav_path}")
    try:
        emb, _ = compute_embedding_from_wav(req.wav_path)
        out_path = Path(req.output_npy)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(out_path), emb)
        return {"message": f"Empreinte sauvée dans {req.output_npy}", "output_npy": req.output_npy}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur création empreinte : {exc}") from exc


@app.post("/build_reference")
def build_reference(req: BuildReferenceRequest) -> dict[str, Any]:
    """Combine une liste de WAVs pour générer l'empreinte moyenne active."""
    global _active_ref_embedding, _active_ref_sample_count
    if not req.wav_paths:
        raise HTTPException(status_code=400, detail="Aucun fichier WAV fourni")

    embs: list[np.ndarray] = []
    valides: list[str] = []
    for p in req.wav_paths:
        if os.path.exists(p):
            try:
                emb, _ = compute_embedding_from_wav(p)
                embs.append(emb)
                valides.append(p)
            except Exception as exc:
                log.warning("Impossible d'extraire l'empreinte de %s : %r", p, exc)

    if not embs:
        raise HTTPException(status_code=400, detail="Aucun fichier WAV valide n'a pu être traité")

    combined_emb = np.mean(embs, axis=0)
    norm = float(np.linalg.norm(combined_emb))
    if norm > 1e-8:
        combined_emb = (combined_emb / norm).astype(np.float32)

    out_file = Path(req.output_npy) if req.output_npy else ACTIVE_REF_PATH
    out_file.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(out_file), combined_emb)

    _active_ref_embedding = combined_emb
    _active_ref_sample_count = len(valides)

    return {
        "message": f"Empreinte de référence mise à jour avec {len(valides)} échantillon(s)",
        "output_npy": str(out_file),
        "samples_count": len(valides),
    }


@app.post("/reload_reference")
def reload_reference() -> dict[str, Any]:
    load_or_init_reference()
    return {
        "status": "ok",
        "reference_loaded": _active_ref_embedding is not None,
        "samples_count": _active_ref_sample_count,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
