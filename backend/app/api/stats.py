"""Agrégats pour le dashboard.

Tout le découpage horaire est fait par PostgreSQL avec `AT TIME ZONE $tz`, donc
la réponse NE DÉPEND PAS du TZ du conteneur (§5.5). Le fuseau est un paramètre
de la requête, jamais une propriété de la session.

Les instants renvoyés restent des instants UTC : seul le REGROUPEMENT est
local. Le client reçoit `tz` en écho pour étiqueter ses axes.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from .. import db
from ..config import settings
from ..schemas import (
    DailyOut,
    DailyPoint,
    HeatCell,
    HeatmapOut,
    HistogramOut,
    HourBucket,
    SummaryOut,
    TimelineOut,
    TimelinePoint,
)
from .events import aware

log = logging.getLogger(__name__)

router = APIRouter(tags=["stats"])

MAX_TIMELINE_POINTS = 5000


def _tz(name: str | None) -> str:
    return name or settings.app_tz


@router.get("/stats/summary", response_model=SummaryOut)
async def summary(
    date: date_type | None = Query(None, description="jour local (défaut : aujourd'hui)"),
    tz: str | None = Query(None),
) -> SummaryOut:
    zone = ZoneInfo(_tz(tz))
    today = datetime.now(zone).date()
    jour = date or today

    row = await db.fetchrow(
        """
        -- sum(bark_count) et NON count(*) : une ligne n'est plus un aboiement
        -- mais un ÉPISODE, qui peut en contenir des dizaines. Compter les
        -- lignes ferait passer « 27 aboiements » à « 2 » du jour au lendemain,
        -- sans qu'aucun test ni aucun message ne le signale. Les lignes
        -- antérieures portent bark_count = 1 par défaut, donc l'historique
        -- reste comparable.
        SELECT coalesce(sum(bark_count), 0) AS count,
               max(dog_score)    AS max_score,
               avg(dog_score)    AS mean_score,
               min(detected_at)  AS first_at,
               max(detected_at)  AS last_at
        FROM events
        WHERE (detected_at AT TIME ZONE $1)::date = $2::date
          AND coalesce(bark_count, 0) > 0
          AND (qc_valid IS NULL OR qc_valid = TRUE)
        """,
        _tz(tz),
        jour,
    )

    # Dénominateur des « par heure » : les heures ÉCOULÉES, pas 24.
    # Diviser par 24 à 9 h du matin fait paraître chaque matin calme et chaque
    # soir alarmant, alors que c'est le même après-midi (§8).
    if jour == today:
        ecoulees = (
            datetime.now(zone) - datetime.combine(jour, datetime.min.time(), tzinfo=zone)
        ).total_seconds() / 3600.0
        heures = min(24.0, max(ecoulees, 1.0 / 60.0))  # jamais 0, jamais > 24
    else:
        heures = 24.0

    veille = await db.fetchval(
        "SELECT coalesce(sum(bark_count), 0) FROM events WHERE (detected_at AT TIME ZONE $1)::date = $2::date AND coalesce(bark_count, 0) > 0 AND (qc_valid IS NULL OR qc_valid = TRUE)",
        _tz(tz),
        jour - timedelta(days=1),
    )
    semaine = await db.fetchval(
        "SELECT coalesce(sum(bark_count), 0) FROM events WHERE (detected_at AT TIME ZONE $1)::date = $2::date AND coalesce(bark_count, 0) > 0 AND (qc_valid IS NULL OR qc_valid = TRUE)",
        _tz(tz),
        jour - timedelta(days=7),
    )

    count = row["count"] or 0
    return SummaryOut(
        date=jour,
        tz=_tz(tz),
        count=count,
        hours_elapsed=round(heures, 3),
        per_hour=round(count / heures, 3) if heures > 0 else 0.0,
        max_dog_score=row["max_score"],
        mean_dog_score=row["mean_score"],
        first_detected_at=row["first_at"],
        last_detected_at=row["last_at"],
        count_prev_day=veille or 0,
        count_prev_week_same_day=semaine or 0,
    )


@router.get("/stats/histogram", response_model=HistogramOut)
async def histogram(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    tz: str | None = Query(None),
) -> HistogramOut:
    now = datetime.now(timezone.utc)
    start = aware(from_, now - timedelta(days=7))
    end = aware(to, now)

    rows = await db.fetch(
        """
        SELECT extract(hour FROM (detected_at AT TIME ZONE $3))::int AS hour,
               coalesce(sum(bark_count), 0) AS count
        FROM events
        WHERE detected_at >= $1 AND detected_at < $2
          AND coalesce(bark_count, 0) > 0
          AND (qc_valid IS NULL OR qc_valid = TRUE)
        GROUP BY 1
        """,
        start,
        end,
        _tz(tz),
    )
    par_heure = {r["hour"]: r["count"] for r in rows}
    # Les 24 buckets sont TOUJOURS renvoyés, y compris vides : un histogramme
    # qui saute les heures creuses décale tout le reste de l'axe.
    buckets = [HourBucket(hour=h, count=par_heure.get(h, 0)) for h in range(24)]
    total = sum(b.count for b in buckets)
    pic = max(buckets, key=lambda b: b.count) if total else None
    return HistogramOut(
        tz=_tz(tz), buckets=buckets, peak_hour=pic.hour if pic else None, total=total
    )


@router.get("/stats/daily", response_model=DailyOut)
async def daily(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    tz: str | None = Query(None),
) -> DailyOut:
    now = datetime.now(timezone.utc)
    start = aware(from_, now - timedelta(days=30))
    end = aware(to, now)

    rows = await db.fetch(
        """
        SELECT d::date AS date, coalesce(sum(e.bark_count), 0) AS count
        FROM generate_series(
                 ($1 AT TIME ZONE $3)::date,
                 ($2 AT TIME ZONE $3)::date,
                 '1 day'
             ) d
        LEFT JOIN events e
               ON (e.detected_at AT TIME ZONE $3)::date = d::date
              AND coalesce(e.bark_count, 0) > 0
              AND (e.qc_valid IS NULL OR e.qc_valid = TRUE)
        GROUP BY d
        ORDER BY d
        """,
        start,
        end,
        _tz(tz),
    )
    # generate_series + LEFT JOIN, et non un GROUP BY sur events : une courbe
    # qui saute les jours calmes trace une ligne droite au-dessus du trou et
    # ment sur la forme de la tendance (§8).
    points = [DailyPoint(date=r["date"], count=r["count"]) for r in rows]
    return DailyOut(tz=_tz(tz), points=points, total=sum(p.count for p in points))


@router.get("/stats/heatmap", response_model=HeatmapOut)
async def heatmap(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    tz: str | None = Query(None),
) -> HeatmapOut:
    now = datetime.now(timezone.utc)
    start = aware(from_, now - timedelta(days=30))
    end = aware(to, now)

    rows = await db.fetch(
        """
        SELECT extract(isodow FROM (detected_at AT TIME ZONE $3))::int AS dow,
               extract(hour   FROM (detected_at AT TIME ZONE $3))::int AS hour,
               coalesce(sum(bark_count), 0) AS count
        FROM events
        WHERE detected_at >= $1 AND detected_at < $2
          AND coalesce(bark_count, 0) > 0
          AND (qc_valid IS NULL OR qc_valid = TRUE)
        GROUP BY 1, 2
        """,
        start,
        end,
        _tz(tz),
    )
    cells = [HeatCell(dow=r["dow"], hour=r["hour"], count=r["count"]) for r in rows]
    total = sum(c.count for c in cells)
    return HeatmapOut(
        tz=_tz(tz),
        cells=cells,
        max_count=max((c.count for c in cells), default=0),
        total=total,
    )


@router.get("/stats/timeline", response_model=TimelineOut)
async def timeline(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    tz: str | None = Query(None),
    max_points: int = Query(2000, ge=1, le=MAX_TIMELINE_POINTS),
) -> TimelineOut:
    now = datetime.now(timezone.utc)
    start = aware(from_, now - timedelta(hours=24))
    end = aware(to, now)

    total = await db.fetchval(
        "SELECT coalesce(sum(bark_count), 0) FROM events WHERE detected_at >= $1 AND detected_at < $2 AND coalesce(bark_count, 0) > 0 AND (qc_valid IS NULL OR qc_valid = TRUE)",
        start,
        end,
    )
    rows = await db.fetch(
        """
        SELECT id,
               (extract(epoch FROM detected_at) * 1000)::bigint AS t,
               dog_score
        FROM events
        WHERE detected_at >= $1 AND detected_at < $2
          AND coalesce(bark_count, 0) > 0
          AND (qc_valid IS NULL OR qc_valid = TRUE)
        ORDER BY dog_score DESC
        LIMIT $3
        """,
        start,
        end,
        max_points,
    )
    # Les N PLUS FORTS, puis remis en ordre chronologique pour l'affichage.
    # Renvoyer les N premiers chronologiquement n'afficherait que janvier et
    # laisserait croire que le reste de l'année est vide (§8).
    points = sorted(
        (TimelinePoint(id=r["id"], t=int(r["t"]), score=float(r["dog_score"])) for r in rows),
        key=lambda p: p.t,
    )
    truncated = False
    if len(rows) == max_points:
        nb_episodes = await db.fetchval(
            "SELECT count(*) FROM events WHERE detected_at >= $1 AND detected_at < $2 AND coalesce(bark_count, 0) > 0 AND (qc_valid IS NULL OR qc_valid = TRUE)",
            start,
            end,
        )
        truncated = (nb_episodes or 0) > len(points)
    return TimelineOut(
        tz=_tz(tz),
        points=points,
        total=total or 0,
        truncated=truncated,
    )
