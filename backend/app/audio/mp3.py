"""Encodage MP3 — sans ffmpeg (§3.4).

lameenc embarque LAME compilé et n'a AUCUNE dépendance : 0,2 Mo de wheel
contre ~350 Mo pour `apt-get install ffmpeg`.

Le seul cas qui forcerait ffmpeg : accepter de l'Opus/WebM depuis un client.
À garder en tête — un changement de transport qui passerait à MediaRecorder
casserait silencieusement l'encodage ici.

Le WAV (écriture comme lecture) vit dans `wav.py`, pas ici : ce module ne fait
qu'encoder du MP3.
"""

from __future__ import annotations

import lameenc
import numpy as np

from .pcm import float32_to_pcm16

BITRATE_KBPS = 64  # ~24 Ko pour 3 s → ~8,8 Go/an au pire (G18)


def encode_mp3(x: np.ndarray, sample_rate: int, bitrate_kbps: int = BITRATE_KBPS) -> bytes:
    """float32 mono → MP3. LAME veut du s16le en entrée."""
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate_kbps)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)  # 2 = proche du maximum, ~2× plus rapide que 0
    encoder.silence()
    return bytes(encoder.encode(float32_to_pcm16(x)) + encoder.flush())
