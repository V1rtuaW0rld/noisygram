"""Sélection du backend — un seul point de branchement."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import ClassifierBackend
from .remote_http import RemoteHttpBackend
from .yamnet_litert import YamnetLitertBackend

log = logging.getLogger(__name__)


def build_classifier(
    settings: Settings,
    classes_cibles: list[str] | None = None,
    seuil: float | None = None,
) -> ClassifierBackend:
    """Construit le backend, depuis le PROJET (voir app/projet.py).

    `classes_cibles` et `seuil` viennent de la ligne du projet. `None` laisse le
    backend appliquer ses propres défauts — c'est ce qui arrive tant qu'aucun
    projet n'a été enregistré, et pour les outils qui n'ont pas de base.
    """
    backend = settings.classifier_backend.strip().lower()
    # Le seuil appartient au projet : celui de `.env` n'est qu'une graine. Un
    # seuil faux ne se voit pas — il fait accepter ou refuser tout, en silence.
    seuil_effectif = seuil if seuil is not None else settings.noisy_threshold

    if backend in ("yamnet_litert", "yamnet-litert", "yamnet"):
        return YamnetLitertBackend(
            model_path=settings.model_path,
            class_map_path=settings.class_map_path,
            threshold=seuil_effectif,
            peak_normalize=settings.peak_normalize,
            classes_cibles=classes_cibles,
        )

    if backend in ("remote_http", "remote-http"):
        if not settings.remote_classifier_url:
            raise ValueError(
                "CLASSIFIER_BACKEND=remote_http exige REMOTE_CLASSIFIER_URL"
            )
        return RemoteHttpBackend(
            url=settings.remote_classifier_url,
            threshold=seuil_effectif,
            timeout_s=settings.remote_classifier_timeout_s,
            classes_cibles=classes_cibles,
        )

    raise ValueError(
        f"CLASSIFIER_BACKEND inconnu : {settings.classifier_backend!r} "
        "(attendu : yamnet_litert ou remote_http)"
    )
