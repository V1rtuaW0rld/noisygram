"""Pont vers un classifieur GPU distant — implémenté, NON sélectionné.

Il existe pour que le passage à une vraie machine GPU, le jour où le volume
le justifiera, ne touche à rien d'autre qu'une variable d'environnement :
CLASSIFIER_BACKEND=remote_http.

Le contrat est volontairement minimal — on envoie du PCM 16 kHz brut en
`application/octet-stream` et on attend un JSON de scores. Un service qui
parle ce contrat peut être n'importe quoi : un YAMNet sur GPU, un modèle
maison, un batch d'inférence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np

from ..audio.pcm import float32_to_pcm16
from .base import ClassificationResult, ClassifierBackend
from .yamnet_litert import NOISY_CLASS_NAMES

log = logging.getLogger(__name__)

REMOTE_TIMEOUT_S = 5.0
TOP_K = 5


class RemoteHttpBackend(ClassifierBackend):
    name = "remote-http"

    def __init__(
        self,
        url: str,
        threshold: float = 0.35,
        timeout_s: float = REMOTE_TIMEOUT_S,
        bark_label: str = "Bark",
        noisy_label: str = "Dog",
        classes_cibles: list[str] | None = None,
    ) -> None:
        # noisy_label ne sert que de repli si le service distant n'expose aucune
        # des classes du groupe surveillé.
        self.url = url
        self.threshold = threshold
        self.timeout_s = timeout_s
        self.bark_label = bark_label
        self.noisy_label = noisy_label
        # Le groupe du PROJET, comme pour le backend local. Sans ça, ce backend
        # noterait sur le groupe canin pendant que le reste de l'appli en
        # surveille un autre — et changer de backend changerait la détection.
        self.classes_cibles: tuple[str, ...] = tuple(classes_cibles or NOISY_CLASS_NAMES)
        self._ready = False

    def load(self) -> None:
        # Aucun modèle local : le service distant possède le sien. On ne fait
        # pas de ping au démarrage — un classifieur distant momentanément
        # injoignable ne doit pas empêcher le serveur de démarrer, il doit
        # produire des `classify_error` clairs.
        self._ready = True
        log.info("classifieur distant configuré : %s", self.url)

    def is_ready(self) -> bool:
        return self._ready

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "model": self.url,
            "bark_index": -1,
            "noisy_index": -1,
            "threshold": self.threshold,
            "window_samples": 0,
            "hop_samples": 0,
        }

    def _post(self, x_16k: np.ndarray) -> dict:
        req = urllib.request.Request(
            self.url,
            data=float32_to_pcm16(x_16k),
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": "16000",
                "User-Agent": "noisygram/remote-classifier",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def classify(self, x_16k: np.ndarray) -> ClassificationResult:
        t0 = time.perf_counter()
        payload = self._post(np.asarray(x_16k, dtype=np.float32).reshape(-1))

        # Format attendu : {"classes": [{"name": "Bark", "index": 70,
        # "score": 0.87}, ...]} — on tolère l'absence d'index.
        classes = payload.get("classes") or []
        if not classes:
            raise ValueError("réponse distante sans 'classes'")

        scores = {c.get("name", ""): float(c.get("score", 0.0)) for c in classes}
        ordered = sorted(classes, key=lambda c: float(c.get("score", 0.0)), reverse=True)

        # Le service distant renvoie des classes nommées : on applique le MÊME
        # critère que le backend local, le max du groupe surveillé. Un modèle
        # distant qui ne les expose pas toutes doit produire le même verdict
        # que YAMNet, sinon changer de backend changerait le seuil.
        groupe = [scores[n] for n in self.classes_cibles if n in scores]
        noisy = max(groupe) if groupe else scores.get(self.noisy_label, 0.0)
        bark = scores.get(self.bark_label)

        top = [
            (str(c.get("name", "?")), int(c.get("index", -1)), float(c.get("score", 0.0)))
            for c in ordered[:TOP_K]
        ]

        return ClassificationResult(
            noisy_score=noisy,
            bark_score=bark,
            # Pas de fenêtrage local : le service distant décide. La moyenne
            # n'a donc pas de sens ici, on la laisse égale au score.
            mean_noisy_score=float(payload.get("mean_noisy_score", noisy)),
            top_classes=top,
            backend=self.name,
            model_version=payload.get("model_version"),
            windows=int(payload.get("windows", 0)),
            processing_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def classify_async(self, x_16k: np.ndarray) -> ClassificationResult:
        # urllib est bloquant : on le sort de la boucle d'événements.
        return await asyncio.to_thread(self.classify, x_16k)


# ------------------------------------------------- analyse à la demande


def analyze_timeline_remote(url: str, chemin, timeout_s: float = 30.0) -> dict:
    """Envoie un WAV à un service d'analyse et rend sa timeline.

    **Multipart écrit à la main**, et ce n'est pas du zèle : le projet n'a
    AUCUN client HTTP (`requirements.txt` n'a ni httpx ni aiohttp ni requests),
    et cette absence est délibérée — l'image ne sort pas sur le réseau en
    exploitation. Ajouter une dépendance pour un seul appel serait disproportionné,
    alors que la stdlib sait le faire.

    L'API visée attend `file=@…` en multipart et rend
    `{total_duration_sec, timeline:[{interval, sound, score}]}`.

    Bloquant : à appeler via `asyncio.to_thread`. Le timeout est bien plus large
    que celui du classifieur — ici on envoie un fichier et une machine distante
    le décode entièrement, alors que `classify` n'envoie qu'une fenêtre.
    """
    chemin = Path(chemin)
    contenu = chemin.read_bytes()
    boundary = f"----noisygram{uuid.uuid4().hex}"

    corps = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{chemin.name}"\r\n'
            ).encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            contenu,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )

    req = urllib.request.Request(
        url,
        data=corps,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(corps)),
            "Accept": "application/json",
            "User-Agent": "noisygram/analyze",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))
