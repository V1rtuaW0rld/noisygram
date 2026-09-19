"""Lecture et purge des événements."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..config import settings
from ..schemas import (
    DeletedOut,
    EventOut,
    EventPage,
    EventSequenceOut,
    EventsSinceOut,
    SequenceEvent,
)
from ..storage import media

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

COLUMNS = (
    "id, detected_at, received_at, client_captured_at, client_id, client_seq, "
    "dog_score, bark_score, mean_dog_score, duration_ms, sample_rate, "
    "mp3_path, mp3_bytes, backend, model_version, top_classes, bark_count, wav_name, "
    "qc_score, is_reference, qc_valid"
)


def aware(dt: datetime | None, fallback: datetime) -> datetime:
    """Un datetime naïf est interprété dans le fuseau de l'application.

    Sans ça, `?from=2026-09-17T00:00:00` (sans décalage) serait comparé à des
    `timestamptz` dans le fuseau du serveur — un décalage silencieux d'une ou
    deux heures selon la saison, et un histogramme faux d'un bucket.
    """
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(settings.app_tz))
    return dt


def row_to_event(row) -> dict:
    data = dict(row)
    data["mp3_url"] = media.url_for(data.pop("mp3_path"))
    top = data.get("top_classes")
    # asyncpg rend le jsonb sous forme de texte : sans ce décodage, le client
    # recevrait une chaîne JSON échappée au lieu d'un tableau.
    if isinstance(top, str):
        try:
            data["top_classes"] = json.loads(top)
        except json.JSONDecodeError:
            data["top_classes"] = None
    return data


@router.get("/events", response_model=EventPage)
async def list_events(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    tz: str | None = Query(None, description="fuseau des regroupements (défaut APP_TZ)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
) -> EventPage:
    now = datetime.now(timezone.utc)
    start = aware(from_, now - timedelta(hours=24))
    end = aware(to, now)

    where = "detected_at >= $1 AND detected_at < $2 AND dog_score >= $3"
    total = await db.fetchval(f"SELECT coalesce(sum(bark_count), 0) FROM events WHERE {where}", start, end, min_score)
    rows = await db.fetch(
        f"SELECT {COLUMNS} FROM events WHERE {where} "
        "ORDER BY detected_at DESC LIMIT $4 OFFSET $5",
        start,
        end,
        min_score,
        limit,
        offset,
    )
    return EventPage(
        items=[row_to_event(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        tz=tz or settings.app_tz,
    )


@router.get("/events/since", response_model=EventsSinceOut)
async def events_since(
    after_id: int | None = Query(None, ge=0, description="dernier id déjà connu"),
    limit: int = Query(50, ge=1, le=200),
) -> EventsSinceOut:
    """Les événements arrivés depuis `after_id` (exclu) — et rien d'autre.

    ⚠️ Déclaré AVANT `/events/{event_id}` : FastAPI apparie dans l'ordre
    d'enregistrement, et « since » ne se convertit pas en entier — c'est la
    route paramétrée qui gagnerait, avec un 422 sur cette URL.

    Sans `after_id`, la réponse est vide et ne sert qu'à livrer `last_id` :
    c'est le point de synchronisation du dashboard au chargement, qui lui évite
    d'avaler tout l'historique pour simplement savoir où il en est.
    """
    if after_id is None:
        return EventsSinceOut(
            items=[], last_id=await db.fetchval("SELECT coalesce(max(id), 0) FROM events"), count=0
        )

    rows = await db.fetch(
        f"SELECT {COLUMNS} FROM events WHERE id > $1 AND coalesce(bark_count, 0) > 0 AND (qc_valid IS NULL OR qc_valid = TRUE) ORDER BY id ASC LIMIT $2",
        after_id,
        limit,
    )
    items = [EventOut(**row_to_event(r)) for r in rows]
    max_global = await db.fetchval("SELECT coalesce(max(id), 0) FROM events")
    if len(rows) == limit and items:
        dernier = items[-1].id
    else:
        dernier = max(after_id, max_global)
    return EventsSinceOut(items=items, last_id=dernier, count=len(items))


_RAFALE_SQL = """
WITH chaine AS (
    SELECT id, detected_at, dog_score, duration_ms, mp3_path, mp3_bytes,
           CASE
             -- IS NULL explicite : la première ligne d'un client est un DÉBUT.
             -- Le ELSE 0 la traiterait comme une continuité dès qu'on bornera
             -- la fenêtre par une date.
             WHEN lag(detected_at) OVER w IS NULL THEN 1
             WHEN detected_at - lag(detected_at) OVER w
                  > make_interval(secs => $2::float8 / 1000.0) THEN 1
             ELSE 0
           END AS debut
    FROM events
    WHERE client_id = $1 AND coalesce(bark_count, 0) > 0
    WINDOW w AS (ORDER BY detected_at, id)
),
-- Les alias de fenêtre ne sont pas visibles dans le WHERE de leur propre
-- SELECT, d'où les trois étages.
numerote AS (
    SELECT *,
           sum(debut) OVER (ORDER BY detected_at, id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rafale
    FROM chaine
),
situe AS (
    SELECT *,
           row_number() OVER (PARTITION BY rafale ORDER BY detected_at, id) AS rang,
           count(*)     OVER (PARTITION BY rafale)                          AS total
    FROM numerote
),
ancre AS (SELECT rafale, detected_at FROM situe WHERE id = $3)
SELECT s.id, s.detected_at, s.dog_score, s.duration_ms, s.mp3_path, s.mp3_bytes,
       s.rang, s.total
FROM situe s, ancre a
WHERE s.rafale = a.rafale
-- Les $4 lignes les PLUS PROCHES de l'ancre, pas les premières : c'est ce qui
-- garantit que l'ancre est dans la réponse même au rang 900 d'une rafale de
-- 1000. Le tri chronologique est refait en Python.
ORDER BY abs(extract(epoch FROM (s.detected_at - a.detected_at))), s.id
LIMIT $4
"""


async def _lignes_rafale(client_id: str, event_id: int, gap_ms: int, limite: int) -> list:
    return await db.fetch(_RAFALE_SQL, client_id, float(gap_ms), event_id, limite + 2)


@router.get("/events/sequence/{event_id}", response_model=EventSequenceOut)
async def event_sequence(
    event_id: int,
    gap_ms: int = Query(
        7000,
        ge=1000,
        le=30000,
        description="écart maximal entre deux détections d'une même rafale",
    ),
    limit: int = Query(60, ge=1, le=240),
) -> EventSequenceOut:
    """La rafale continue qui contient cet événement, prête à être écoutée.

    ⚠️ Déclarée AVANT `/events/{event_id}`, par cohérence avec `since` : dès
    qu'une route littérale tient sur un seul segment, l'ordre décide.

    Quand un événement sonore se répète sans discontinuer, le client déclenche toutes les 3 s
    et les segments de 3 s se touchent à quelques millisecondes près. Remis
    bout à bout, ils forment l'enregistrement continu — c'est ce que cette
    route permet d'écouter d'un seul « play ».

    Le chaînage se fait sur `detected_at` avec 7 s par défaut, et NON 3 ou 4 :
    la table ne contient que les événements ACCEPTÉS, donc un seul événement
    refusé au milieu d'une rafale laisse un trou de 6 s. Un seuil trop serré la
    couperait en deux à chaque refus, c'est-à-dire en permanence.
    """
    ancre = await db.fetchrow(
        f"SELECT {COLUMNS}, bark_count FROM events WHERE id = $1", event_id
    )
    if ancre is None:
        raise HTTPException(status_code=404, detail=f"événement {event_id} inconnu")

    # Un ÉPISODE est déjà un enregistrement continu : son fichier EST la
    # rafale. Le chaîner avec ses voisins recollerait des scènes distinctes —
    # exactement ce que le passage au flux a supprimé. Un épisode se renvoie
    # donc SEUL, et le lecteur du dashboard le joue tel quel, sans modification.
    #
    # Les lignes antérieures (clips de 3 s, bark_count = 1) gardent le chaînage
    # d'origine : l'historique reste écoutable comme avant.
    episode = (ancre["bark_count"] or 1) > 1
    client_id = ancre["client_id"]

    if episode or client_id is None:
        # Sans identifiant de client, aucun chaînage n'est possible non plus :
        # on renvoie l'événement seul plutôt qu'une erreur — il existe.
        lignes = [dict(ancre, rang=1, total=1)]
    else:
        lignes = await _lignes_rafale(client_id, event_id, gap_ms, limit)

    # Les lignes arrivent classées par distance à l'ancre ; on les remet dans
    # l'ordre du temps, qui est l'ordre d'écoute.
    lignes.sort(key=lambda r: (r["detected_at"], r["id"]))
    total = int(lignes[0]["total"]) if lignes else 1

    # Fenêtre centrée sur l'ancre : on entend toujours le contexte du clic, et
    # jamais un préfixe arbitraire d'une rafale trop longue.
    idx = next((i for i, r in enumerate(lignes) if r["id"] == event_id), 0)
    if len(lignes) > limit:
        debut = max(0, min(idx - limit // 2, len(lignes) - limit))
    else:
        debut = 0
    retenues = lignes[debut : debut + limit]

    events: list[SequenceEvent] = []
    offset = 0
    for r in retenues:
        events.append(
            SequenceEvent(
                id=r["id"],
                detected_at=r["detected_at"],
                dog_score=r["dog_score"],
                duration_ms=r["duration_ms"],
                mp3_url=media.url_for(r["mp3_path"]),
                mp3_bytes=r["mp3_bytes"],
                rang=r["rang"],
                offset_ms=offset,
            )
        )
        offset += r["duration_ms"]

    ancre_clip = next((e for e in events if e.id == event_id), events[-1])
    return EventSequenceOut(
        anchor_id=event_id,
        anchor_rang=ancre_clip.rang,
        anchor_offset_ms=ancre_clip.offset_ms,
        client_id=client_id,
        gap_ms=gap_ms,
        started_at=events[0].detected_at,
        ended_at=events[-1].detected_at,
        duration_ms=offset,
        total=total,
        count=len(events),
        truncated=total > len(events),
        events=events,
    )


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: int) -> EventOut:
    row = await db.fetchrow(f"SELECT {COLUMNS} FROM events WHERE id = $1", event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"événement {event_id} inconnu")
    return EventOut(**row_to_event(row))


@router.delete("/events/{event_id}", response_model=DeletedOut)
async def delete_event(event_id: int) -> DeletedOut:
    """Purge d'un faux positif évident, fichier compris."""
    row = await db.fetchrow(
        "DELETE FROM events WHERE id = $1 RETURNING mp3_path", event_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"événement {event_id} inconnu")
    # La ligne d'abord, le fichier ensuite. Dans cet ordre, un échec de
    # suppression du fichier laisse un orphelin sur disque — invisible et
    # inoffensif. Dans l'ordre inverse, il laisserait une ligne pointant vers
    # un MP3 absent, donc un dashboard qui propose une lecture qui échoue.
    removed = media.delete(settings.media_dir, row["mp3_path"])
    if not removed:
        log.warning(
            "événement %d supprimé, mais le fichier %s était déjà absent",
            event_id,
            row["mp3_path"],
        )
    return DeletedOut(id=event_id, deleted=True, mp3_removed=removed)


@router.post("/events/{event_id}/disapprove")
async def disapprove_event(event_id: int):
    """Marquer un événement comme faux / désapprouvé par l'utilisateur."""
    row = await db.fetchrow(
        "UPDATE events SET bark_count = 0, backend = 'refused/user_rejected', is_reference = FALSE, qc_valid = FALSE "
        "WHERE id = $1 RETURNING id",
        event_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"événement {event_id} inconnu")
    return {"id": event_id, "status": "disapproved", "bark_count": 0, "qc_valid": False}


@router.post("/events/{event_id}/approve")
async def approve_event(event_id: int):
    """Réhabiliter un événement précédemment marqué comme faux."""
    row = await db.fetchrow(
        "UPDATE events SET bark_count = 1, backend = 'episode/silence', qc_valid = TRUE "
        "WHERE id = $1 RETURNING id",
        event_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"événement {event_id} inconnu")
    return {"id": event_id, "status": "approved", "bark_count": 1, "qc_valid": True}
