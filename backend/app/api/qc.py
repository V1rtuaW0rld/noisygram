"""API REST pour le Quality Center (QC) acoustique."""

import asyncio
import logging
import wave
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db, qc_client
from ..config import settings
from ..storage import media

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/qc", tags=["qc"])


class GridUpdate(BaseModel):
    grid: dict[str, int | float]


class SnippetExtractRequest(BaseModel):
    wav_name: str
    debut_s: float
    fin_s: float
    sound: str | None = None
    score: float | None = None
    event_id: int | None = None


SNIPPETS_DIR = Path("/data/reference/snippets")
DEFAULT_REF_WAV = "/data/reference/Aboiements.wav"


async def get_active_reference_wav_paths() -> list[str]:
    """Rassemble tous les extraits actifs et la baseline pour le microservice QC."""
    rows = await db.fetch("SELECT snippet_filename FROM qc_snippets ORDER BY id ASC")
    wav_paths = [f"/data/reference/snippets/{r['snippet_filename']}" for r in rows]
    # Toujours inclure la référence baseline initiale si elle existe
    wav_paths.insert(0, DEFAULT_REF_WAV)
    return wav_paths


@router.get("/config")
async def get_qc_config() -> dict[str, Any]:
    grid = await qc_client.get_grid()
    health = await qc_client.get_qc_health()
    snippet_count = await db.fetchval("SELECT count(*) FROM qc_snippets") or 0
    return {
        "enabled": settings.qc_enabled,
        "grid": grid,
        "health": health,
        "snippets_count": snippet_count,
    }


@router.put("/config")
async def update_qc_config(payload: GridUpdate) -> dict[str, Any]:
    saved = await qc_client.save_grid(payload.grid)
    return {
        "saved": True,
        "grid": saved,
    }


@router.get("/candidates")
async def list_qc_candidates(limit: int = 500) -> list[dict[str, Any]]:
    """Liste les enregistrements scorés par YAMNet pour le Quality Center."""
    rows = await db.fetch(
        """
        SELECT e.id, e.detected_at, e.dog_score, e.qc_score, e.duration_ms, e.mp3_path,
               e.bark_count, e.wav_name, e.is_reference, e.backend,
               COUNT(s.id)::int AS snippets_count
        FROM events e
        LEFT JOIN qc_snippets s ON s.event_id = e.id
        WHERE (e.backend NOT LIKE 'refused/%' OR e.backend = 'refused/user_rejected')
        GROUP BY e.id
        ORDER BY e.id ASC
        LIMIT $1
        """,
        limit,
    )
    results = []
    for r in rows:
        d = dict(r)
        d["mp3_url"] = media.url_for(d.pop("mp3_path"))
        wav_name = d.get("wav_name")
        wav_exists = False
        if wav_name:
            p = settings.ondemand_dir / wav_name
            wav_exists = p.exists()
        d["wav_exists"] = wav_exists
        results.append(d)
    return results


@router.get("/snippets")
async def list_snippets(wav_name: str | None = None) -> list[dict[str, Any]]:
    """Liste tous les extraits Best-of de référence actifs."""
    if wav_name:
        rows = await db.fetch(
            """
            SELECT id, event_id, wav_name, snippet_filename, debut_s, fin_s, duration_s, sound, score, created_at
            FROM qc_snippets
            WHERE wav_name = $1
            ORDER BY debut_s ASC
            """,
            wav_name,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, event_id, wav_name, snippet_filename, debut_s, fin_s, duration_s, sound, score, created_at
            FROM qc_snippets
            ORDER BY id ASC
            """
        )

    results = []
    for r in rows:
        d = dict(r)
        d["audio_url"] = f"/api/qc/snippets/{d['id']}/audio"
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        results.append(d)
    return results


@router.post("/snippets/extract")
async def extract_reference_snippet(req: SnippetExtractRequest) -> dict[str, Any]:
    """Découpe un segment sonore d'un enregistrement et l'ajoute au catalogue Best-of."""
    src_path = settings.ondemand_dir / req.wav_name
    if not src_path.exists():
        src_path = Path("/data/debug") / req.wav_name
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"Fichier source introuvable : {req.wav_name}")

    if req.fin_s <= req.debut_s:
        raise HTTPException(status_code=400, detail="Intervalle invalide : fin_s <= debut_s")

    # Résolution de l'event_id si non fourni
    ev_id = req.event_id
    if not ev_id:
        row_ev = await db.fetchrow("SELECT id FROM events WHERE wav_name = $1 LIMIT 1", req.wav_name)
        if row_ev:
            ev_id = row_ev["id"]
        else:
            ev_id = 0

    try:
        with wave.open(str(src_path), "rb") as w_in:
            sr = w_in.getframerate()
            n_frames = w_in.getnframes()
            channels = w_in.getnchannels()
            sampwidth = w_in.getsampwidth()

            start_sample = max(0, min(int(req.debut_s * sr), n_frames))
            end_sample = max(start_sample, min(int(req.fin_s * sr), n_frames))
            samples_to_read = end_sample - start_sample

            if samples_to_read <= 0:
                raise HTTPException(status_code=400, detail="Durée de segment nulle")

            w_in.setpos(start_sample)
            raw_audio = w_in.readframes(samples_to_read)

        SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
        ms_debut = int(round(req.debut_s * 1000))
        ms_fin = int(round(req.fin_s * 1000))
        snippet_filename = f"ref_{ev_id}_{ms_debut}_{ms_fin}.wav"
        dst_path = SNIPPETS_DIR / snippet_filename

        with wave.open(str(dst_path), "wb") as w_out:
            w_out.setnchannels(channels)
            w_out.setsampwidth(sampwidth)
            w_out.setframerate(sr)
            w_out.writeframes(raw_audio)

        duration_s = round(samples_to_read / float(sr), 3)

        row = await db.fetchrow(
            """
            INSERT INTO qc_snippets (event_id, wav_name, snippet_filename, debut_s, fin_s, duration_s, sound, score)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (snippet_filename) DO UPDATE
            SET debut_s = EXCLUDED.debut_s, fin_s = EXCLUDED.fin_s, duration_s = EXCLUDED.duration_s,
                sound = EXCLUDED.sound, score = EXCLUDED.score
            RETURNING id, event_id, wav_name, snippet_filename, debut_s, fin_s, duration_s, sound, score, created_at
            """,
            ev_id if ev_id > 0 else None,
            req.wav_name,
            snippet_filename,
            req.debut_s,
            req.fin_s,
            duration_s,
            req.sound,
            req.score,
        )

        # Mettre à jour l'événement parent
        if ev_id > 0:
            await db.execute("UPDATE events SET is_reference = TRUE WHERE id = $1", ev_id)

        # Reconstruire l'empreinte QC
        wav_paths = await get_active_reference_wav_paths()
        qc_res = await qc_client.build_reference(wav_paths)

        d = dict(row)
        d["audio_url"] = f"/api/qc/snippets/{d['id']}/audio"
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None

        return {
            "snippet": d,
            "qc_build": qc_res,
            "total_snippets": len(wav_paths) - 1,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Erreur lors de l'extraction de segment pour %s : %r", req.wav_name, exc)
        raise HTTPException(status_code=500, detail=f"Erreur d'extraction audio : {exc}") from exc


@router.delete("/snippets/{snippet_id}")
async def delete_reference_snippet(snippet_id: int) -> dict[str, Any]:
    """Supprime un extrait du catalogue Best-of et reconstruit l'empreinte QC."""
    row = await db.fetchrow(
        "DELETE FROM qc_snippets WHERE id = $1 RETURNING id, event_id, snippet_filename",
        snippet_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Extrait n°{snippet_id} introuvable")

    snippet_file = SNIPPETS_DIR / row["snippet_filename"]
    if snippet_file.exists():
        snippet_file.unlink(missing_ok=True)

    # Si plus aucun snippet pour cet event, retirer le drapeau is_reference si souhaité
    ev_id = row["event_id"]
    if ev_id:
        reste = await db.fetchval("SELECT count(*) FROM qc_snippets WHERE event_id = $1", ev_id)
        if reste == 0:
            await db.execute("UPDATE events SET is_reference = FALSE WHERE id = $1", ev_id)

    wav_paths = await get_active_reference_wav_paths()
    qc_res = await qc_client.build_reference(wav_paths)

    return {
        "deleted": True,
        "id": snippet_id,
        "qc_build": qc_res,
        "total_snippets": len(wav_paths) - 1,
    }


@router.get("/snippets/{snippet_id}/audio")
async def get_snippet_audio(snippet_id: int) -> FileResponse:
    """Sert le fichier audio découpé d'un snippet de référence."""
    row = await db.fetchrow("SELECT snippet_filename FROM qc_snippets WHERE id = $1", snippet_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Extrait {snippet_id} introuvable")

    p = SNIPPETS_DIR / row["snippet_filename"]
    if not p.exists():
        raise HTTPException(status_code=404, detail="Fichier audio du snippet introuvable sur disque")

    return FileResponse(p, media_type="audio/wav")


@router.post("/rebuild")
async def rebuild_reference_embedding() -> dict[str, Any]:
    """Force le recalcul de l'empreinte vectorielle moyenne du QC."""
    wav_paths = await get_active_reference_wav_paths()
    qc_res = await qc_client.build_reference(wav_paths)
    return {
        "rebuilt": True,
        "qc_build": qc_res,
        "total_snippets": len(wav_paths) - 1,
    }


@router.post("/reference/toggle/{event_id}")
async def toggle_reference(event_id: int) -> dict[str, Any]:
    """Bascule le statut référence d'un enregistrement."""
    row = await db.fetchrow(
        """
        UPDATE events
        SET is_reference = NOT is_reference
        WHERE id = $1
        RETURNING id, is_reference, wav_name
        """,
        event_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Événement {event_id} introuvable")

    wav_paths = await get_active_reference_wav_paths()
    qc_res = await qc_client.build_reference(wav_paths)
    return {
        "event_id": event_id,
        "is_reference": row["is_reference"],
        "qc_build": qc_res,
    }


@router.post("/eval-all")
async def eval_all_events() -> dict[str, Any]:
    """Évalue rétroactivement tous les événements disposant d'un WAV avec le QC actuel."""
    rows = await db.fetch(
        """
        SELECT id, duration_ms, wav_name, backend, qc_valid
        FROM events
        WHERE wav_name IS NOT NULL
        ORDER BY id ASC
        """
    )
    evaluated = 0
    updated = 0
    for r in rows:
        w_nom = r["wav_name"]
        p = settings.ondemand_dir / w_nom
        if not p.exists():
            continue
        evaluated += 1
        qc_sc, qc_val = await qc_client.evaluate_qc(str(p), r["duration_ms"])
        if qc_sc is not None:
            # Si l'utilisateur a explicitement rejeté l'événement, on garde qc_valid = FALSE
            is_user_rejected = r["backend"] == "refused/user_rejected"
            final_valid = False if is_user_rejected else qc_val
            await db.execute(
                """
                UPDATE events
                SET qc_score = $1, qc_valid = $2
                WHERE id = $3
                """,
                qc_sc,
                final_valid,
                r["id"],
            )
            updated += 1

    return {"total_wav_events": len(rows), "evaluated": evaluated, "updated": updated}

