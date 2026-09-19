"""Rééchantillonnage vers 16 kHz — soxr, jamais le navigateur.

Le client envoie la fréquence NATIVE de son AudioContext (§5.1). Il ne
rééchantillonne PAS lui-même : le rééchantillonneur de Chrome est de qualité
variable selon la plateforme, soxr ne l'est pas.

C'est la correction du risque G4 : si on supposait 48 kHz en dur alors que le
micro tourne à 44,1 kHz, l'audio serait transposé d'environ 9 % et TOUT
classerait au hasard — sans erreur, sans exception, sans rien dans le journal.
"""

from __future__ import annotations

import numpy as np
import soxr

TARGET_SR = 16_000


def resample_to_16k(x: np.ndarray, src_sr: int) -> np.ndarray:
    if src_sr == TARGET_SR:
        return np.ascontiguousarray(x, dtype=np.float32)
    return np.ascontiguousarray(
        soxr.resample(x, src_sr, TARGET_SR, quality="HQ"), dtype=np.float32
    )
