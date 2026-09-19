"""Copie sur disque de l'audio EXACTEMENT tel qu'il part au classifieur.

Existe pour répondre à une question qu'aucun journal ne tranche : « qu'est-ce
que YAMNet a réellement entendu ? ». Les scores disent qu'il refuse ; ils ne
disent pas si c'est parce que l'audio est mauvais ou parce que le modèle n'y
reconnaît rien. Un WAV qu'on peut écouter répond en trois secondes.

C'est un outil de diagnostic, pas une fonctionnalité : on l'active le temps
d'une enquête, et il écrit dans un dossier monté depuis l'hôte pour qu'on
puisse l'ouvrir sans passer par `docker cp`.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .audio.wav import wav_bytes

log = logging.getLogger(__name__)

_nom_sur = re.compile(r"[^A-Za-z0-9._-]+")


def _assainir(nom: str) -> str:
    return _nom_sur.sub("-", nom)[:80]


def dump(
    x: np.ndarray,
    label: str,
    racine: Path | None,
    actif: bool,
    garder: int = 0,
) -> Path | None:
    """Écrit `x` (float32, 16 kHz) en WAV, et rend le chemin écrit.

    `garder` est le nombre de fichiers à conserver ; **0 signifie « ne supprime
    jamais rien »**, ce qui est la valeur voulue pendant une enquête — on veut
    pouvoir réécouter un épisode d'il y a une heure.

    Ne lève JAMAIS : un dump de diagnostic qui fait échouer la classification
    qu'il observe serait le comble de l'outil inutile.
    """
    if not actif or racine is None or x.size == 0:
        return None
    try:
        racine.mkdir(parents=True, exist_ok=True)
        horodate = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
        chemin = racine / f"{horodate}_{_assainir(label)}_debug.wav"
        chemin.write_bytes(wav_bytes(x, 16000))
        if garder > 0:
            _elaguer(racine, garder)
        return chemin
    except Exception as exc:  # noqa: BLE001
        log.warning("dump de debug impossible : %s", exc)
        return None


def _elaguer(racine: Path, garder: int) -> None:
    fichiers = sorted(racine.glob("*_debug.wav"), key=lambda p: p.stat().st_mtime)
    for vieux in fichiers[:-garder]:
        try:
            os.unlink(vieux)
        except OSError:
            pass
