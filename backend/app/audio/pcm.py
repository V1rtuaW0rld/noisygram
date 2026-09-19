"""Conversion PCM s16le ↔ float32.

Aucun décodeur : le client envoie du PCM brut, sans en-tête ni compression
(§5.1). C'est ce qui garantit que le pré-roll est correct par construction et
qu'on n'a jamais besoin de ffmpeg.
"""

from __future__ import annotations

import numpy as np

INT16_SCALE = 32768.0


def pcm16_to_float32(payload: bytes, channels: int = 1) -> np.ndarray:
    """s16le brut → float32 mono dans [-1, 1].

    Le downmix (L+R)/2 est fait ici aussi, par sécurité : le client le fait
    déjà dans le worklet, mais un client tiers qui enverrait du stéréo ne doit
    pas produire du silence numérique (G10).
    """
    if len(payload) % 2 != 0:
        raise ValueError(f"payload PCM de longueur impaire : {len(payload)} octets")

    x = np.frombuffer(payload, dtype="<i2")
    if channels > 1:
        if x.size % channels != 0:
            raise ValueError(
                f"{x.size} échantillons non divisibles par {channels} canaux"
            )
        # float32 AVANT la moyenne : .mean() sur des int16 tronquerait.
        x = x.reshape(-1, channels).astype(np.float32).mean(axis=1)
        return x / INT16_SCALE

    return x.astype(np.float32) / INT16_SCALE


def float32_to_pcm16(x: np.ndarray) -> bytes:
    """float32 [-1, 1] → s16le. Écrête au lieu de replier : un dépassement de
    gain doit s'entendre comme une saturation, pas comme un craquement
    inversé."""
    clipped = np.clip(x, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
