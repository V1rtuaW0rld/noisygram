"""Client pour le microservice Quality Center (QC) acoustique."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import db
from .config import settings

log = logging.getLogger(__name__)

DEFAULT_GRID = {
    "<=0.5s": 19,
    "<=1s": 25,
    "<=2s": 33,
    "<=3s": 43,
    "<=4s": 47,
    "<=5s": 53,
    "<=6s": 60,
    "<=7s": 65,
    "<=10s": 70,
    ">10s": 75,
}

_cached_grid: dict[str, int | float] | None = None


def get_required_threshold(duration_ms: int, grid: dict[str, Any] | None = None) -> float:
    """Résout le seuil exigé (%) en fonction de la durée de l'échantillon sonore."""
    g = grid or _cached_grid or DEFAULT_GRID
    d_s = duration_ms / 1000.0

    if d_s <= 0.5:
        return float(g.get("<=0.5s", 19))
    if d_s <= 1.0:
        return float(g.get("<=1s", 25))
    if d_s <= 2.0:
        return float(g.get("<=2s", 33))
    if d_s <= 3.0:
        return float(g.get("<=3s", 43))
    if d_s <= 4.0:
        return float(g.get("<=4s", 47))
    if d_s <= 5.0:
        return float(g.get("<=5s", 53))
    if d_s <= 6.0:
        return float(g.get("<=6s", 60))
    if d_s <= 7.0:
        return float(g.get("<=7s", 65))
    if d_s <= 10.0:
        return float(g.get("<=10s", 70))
    return float(g.get(">10s", 75))


async def get_grid() -> dict[str, Any]:
    global _cached_grid
    try:
        val = await db.fetchval("SELECT value FROM qc_config WHERE key = 'duration_thresholds'")
        if val:
            if isinstance(val, str):
                _cached_grid = json.loads(val)
            else:
                _cached_grid = dict(val)
            return _cached_grid
    except Exception as exc:
        log.warning("Impossible de lire qc_config depuis la base : %r", exc)
    _cached_grid = dict(DEFAULT_GRID)
    return _cached_grid


async def save_grid(grid: dict[str, Any]) -> dict[str, Any]:
    global _cached_grid
    _cached_grid = dict(grid)
    json_val = json.dumps(grid)
    await db.execute(
        """
        INSERT INTO qc_config (key, value, updated_at)
        VALUES ('duration_thresholds', $1::jsonb, now())
        ON CONFLICT (key) DO UPDATE SET value = $1::jsonb, updated_at = now()
        """,
        json_val,
    )
    return _cached_grid


def _http_req_sync(url: str, method: str = "GET", data: dict | None = None, timeout: float = 10.0) -> dict | None:
    try:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read()
                if raw:
                    return json.loads(raw.decode("utf-8"))
                return {}
    except Exception as exc:
        log.warning("Échec HTTP %s %s : %r", method, url, exc)
    return None


async def verify_wav(wav_path: str) -> dict[str, Any] | None:
    """Interroge le microservice QC pour obtenir le score de similarité cosinus."""
    if not settings.qc_enabled:
        return None

    url = f"{settings.qc_url.rstrip('/')}/verify"
    return await asyncio.to_thread(
        _http_req_sync, url, "POST", {"wav_path": str(wav_path)}, settings.qc_timeout_s
    )


async def build_reference(wav_paths: list[str]) -> dict[str, Any] | None:
    """Demande au service QC de combiner une liste d'échantillons en empreinte active."""
    url = f"{settings.qc_url.rstrip('/')}/build_reference"
    return await asyncio.to_thread(
        _http_req_sync, url, "POST", {"wav_paths": wav_paths}, 30.0
    )


async def get_qc_health() -> dict[str, Any]:
    url = f"{settings.qc_url.rstrip('/')}/health"
    res = await asyncio.to_thread(_http_req_sync, url, "GET", None, 3.0)
    return res or {"status": "offline", "model_loaded": False, "reference_loaded": False}


async def evaluate_qc(wav_path: str, duration_ms: int) -> tuple[float | None, bool | None]:
    """Évalue un audio contre l'empreinte QC et la grille de seuils.

    Renvoie (score_qc, is_valid).
    """
    resp = await verify_wav(wav_path)
    if not resp or "percentage" not in resp:
        return None, None
    pct = float(resp["percentage"])
    qc_score = float(resp.get("score", pct / 100.0))
    grid = await get_grid()
    req_threshold = get_required_threshold(duration_ms, grid)
    is_valid = pct >= req_threshold
    return qc_score, is_valid
