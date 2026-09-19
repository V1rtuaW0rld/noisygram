"""Sélection du backend — un seul point de branchement."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import ClassifierBackend
from .remote_http import RemoteHttpBackend
from .yamnet_litert import YamnetLitertBackend

log = logging.getLogger(__name__)


def build_classifier(settings: Settings) -> ClassifierBackend:
    backend = settings.classifier_backend.strip().lower()

    if backend in ("yamnet_litert", "yamnet-litert", "yamnet"):
        return YamnetLitertBackend(
            model_path=settings.model_path,
            class_map_path=settings.class_map_path,
            threshold=settings.noisy_threshold,
            peak_normalize=settings.peak_normalize,
        )

    if backend in ("remote_http", "remote-http"):
        if not settings.remote_classifier_url:
            raise ValueError(
                "CLASSIFIER_BACKEND=remote_http exige REMOTE_CLASSIFIER_URL"
            )
        return RemoteHttpBackend(
            url=settings.remote_classifier_url,
            threshold=settings.noisy_threshold,
            timeout_s=settings.remote_classifier_timeout_s,
        )

    raise ValueError(
        f"CLASSIFIER_BACKEND inconnu : {settings.classifier_backend!r} "
        "(attendu : yamnet_litert ou remote_http)"
    )
