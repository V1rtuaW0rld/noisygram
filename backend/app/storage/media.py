"""Stockage des MP3 sur disque.

Arborescence par date LOCALE (`APP_TZ`), pas UTC : c'est un humain qui ira
regarder dans `/data/media/` pour retrouver l'aboiement de cette nuit, et il
cherchera au 17 septembre, pas au 16 à 22 h UTC. L'instant stocké en base, lui,
reste en UTC — seule l'arborescence est locale, et elle n'est jamais qu'une
commodité de rangement puisque le chemin exact est en base.

Croissance : ~24 Ko par événement en 64 kbps mono. Pire cas réaliste ~1 000
événements/jour ≈ 24 Mo/jour ≈ 8,8 Go/an (G18).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def root(media_dir: Path) -> Path:
    return media_dir


def ensure_root(media_dir: Path) -> None:
    media_dir.mkdir(parents=True, exist_ok=True)


def local_date_dir(detected_at_utc: datetime, app_tz: str) -> str:
    return detected_at_utc.astimezone(ZoneInfo(app_tz)).strftime("%Y/%m/%d")


def mp3_relpath(detected_at_utc: datetime, event_id: int, app_tz: str) -> str:
    """Chemin relatif du MP3 d'un événement accepté.

    Nommé par l'identifiant de l'événement, pas par un horodatage : c'est ce
    qui permet de passer de la ligne en base au fichier sans ambiguïté, et
    réciproquement. D'où la réservation de l'identifiant AVANT l'écriture.
    """
    return f"{local_date_dir(detected_at_utc, app_tz)}/{event_id:06d}.mp3"


def rejected_relpath(detected_at_utc: datetime, seq: int, app_tz: str) -> str:
    """Chemin d'un WAV refusé, quand SAVE_REJECTED=true.

    Sous `rejected/`, donc jamais servi par `/media` : ces fichiers servent à
    régler le seuil, pas à être écoutés depuis le dashboard.
    """
    stamp = detected_at_utc.astimezone(ZoneInfo(app_tz)).strftime("%H%M%S")
    return f"rejected/{local_date_dir(detected_at_utc, app_tz)}/{stamp}-{seq:06d}.wav"


SPOOL_DIR = ".tmp"


def spool_relpath(session_id: str, seq: int, app_tz: str, now_utc: datetime) -> str:
    """Chemin du temporaire d'un épisode en cours d'écriture.

    Sous `media_dir/.tmp`, donc sur le MÊME volume que le MP3 final : `os.replace`
    reste atomique au moment de publier. Un temporaire sur un autre volume
    devrait être copié, et la copie n'est pas atomique.

    Nommé par session et par seq plutôt que par date : deux épisodes du même
    client ne peuvent pas se marcher dessus, même si leurs horodatages se
    ressemblent.
    """
    return f"{SPOOL_DIR}/{local_date_dir(now_utc, app_tz)}/{session_id}-{seq:06d}.pcm"


def sweep_spool(media_dir: Path, older_than_s: int = 3600) -> int:
    """Supprime les temporaires abandonnés au démarrage.

    Un `.part` ou un `.pcm` oublié, c'est un processus tué en pleine écriture :
    sans ce balayage, ils s'accumulent en silence — et ils pèsent maintenant
    plusieurs mégaoctets chacun, là où un segment en laissait trente mille fois
    moins.
    """
    racine = media_dir / SPOOL_DIR
    if not racine.is_dir():
        return 0
    limite = time.time() - older_than_s
    n = 0
    for chemin in racine.rglob("*"):
        try:
            if chemin.is_file() and chemin.stat().st_mtime < limite:
                chemin.unlink()
                n += 1
        except OSError:
            continue
    # Les .part de write_bytes vivent à côté des MP3, pas sous .tmp : ceux-là
    # sont plus récents qu'une heure ou n'existent pas.
    for chemin in media_dir.rglob("*.part"):
        try:
            if chemin.stat().st_mtime < limite:
                chemin.unlink()
                n += 1
        except OSError:
            continue
    return n


def write_bytes(media_dir: Path, relpath: str, data: bytes) -> int:
    """Écriture atomique : `.part` puis `os.replace`.

    Sans ça, un `/media/...mp3` pourrait être servi à moitié écrit — un
    fichier tronqué qui se lit comme un MP3 corrompu, ce qui enverrait
    chercher le bug du côté de l'encodeur.
    """
    target = media_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return len(data)


def delete(media_dir: Path, relpath: str) -> bool:
    try:
        (media_dir / relpath).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("suppression impossible de %s : %r", relpath, exc)
        return False


def url_for(relpath: str) -> str:
    return f"/media/{relpath}"


def free_bytes(media_dir: Path) -> int | None:
    """Espace libre du volume qui porte les médias.

    `statvfs` sur le dossier lui-même : c'est le bon volume même quand
    /data/media est un montage distinct de la racine.
    """
    try:
        st = os.statvfs(media_dir if media_dir.exists() else media_dir.parent)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None
