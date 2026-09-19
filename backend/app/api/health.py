"""Liveness — utilisé par le healthcheck du compose.

Ne touche JAMAIS au classifieur : il est derrière un verrou et pourrait être en
plein calcul, et un healthcheck qui attend l'inférence ferait expirer le
conteneur au mauvais moment. On lit son état, jamais on ne l'interroge.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Request

from .. import db
from ..config import settings
from ..schemas import HealthOut
from ..storage import media

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(request: Request) -> HealthOut:
    detail: list[str] = []

    # --- base de données ---
    try:
        await db.fetchval("SELECT 1")
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        database = "error"
        detail.append(f"base : {exc}")

    # --- dossier des médias ---
    media_dir = settings.media_dir
    try:
        exists = media_dir.is_dir()
        writable = exists and os.access(media_dir, os.W_OK)
        media_state = "ok" if (exists and writable) else "error"
        if not exists:
            detail.append(f"dossier média absent : {media_dir}")
        elif not writable:
            detail.append(f"dossier média non inscriptible : {media_dir}")
    except OSError as exc:
        media_state, writable = "error", False
        detail.append(f"dossier média : {exc}")

    classifier = getattr(request.app.state, "classifier", None)
    try:
        info = classifier.describe() if classifier is not None else {}
        ready = bool(classifier is not None and classifier.is_ready())
    except Exception:  # noqa: BLE001
        info, ready = {}, False

    # `degraded` sort en HTTP 200, pas en 503 : un 503 ferait redémarrer le
    # conteneur en boucle sur un hoquet transitoire de la base, alors que c'est
    # au dashboard d'afficher la dégradation (§8).
    statut = "ok" if (database == "ok" and media_state == "ok") else "degraded"

    return HealthOut(
        status=statut,
        version=settings.server_version,
        role=settings.app_role,
        uptime_s=round(time.time() - getattr(request.app.state, "started_at", time.time()), 3),
        database=database,
        media_dir=media_state,
        media_writable=bool(writable),
        classifier_ready=ready,
        noisy_index=info.get("noisy_index"),
        bark_index=info.get("bark_index"),
        threshold=settings.noisy_threshold,
        disk_free_bytes=media.free_bytes(media_dir),
        detail=" ; ".join(detail) if detail else None,
    )
