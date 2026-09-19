"""Lecture et écriture WAV — le pendant entrant de `mp3.py`.

Un seul lecteur pour tout le service, pour la même raison qu'il n'y a qu'un
seul chemin d'écriture (`mp3.py`) : trois lecteurs divergents finissent par ne
plus gérer les mêmes largeurs d'échantillon, et le symptôme est un tableau vide
ou un facteur d'échelle faux — jamais une exception.

Les deux lecteurs ne sont pas redondants :

  `read_wav_mono`    rend de l'int16 à la fréquence NATIVE. C'est ce qu'il faut
                     pour tester le chemin réseau, où le rééchantillonnage fait
                     justement partie de ce qu'on vérifie.

  `read_wav_float32` rend du float32 et accepte 8/16/32 bits. C'est ce qu'il
                     faut pour analyser un fichier de terrain, qui vient d'un
                     outil quelconque et pas d'un de nos producteurs.

Le module n'embarque aucun décodeur MP3, volontairement (§3.4) : convertir
avant de déposer.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

from .pcm import float32_to_pcm16

# Échelle int16 → float32. 32768 et non 32767 : c'est le maximum de |−32768|,
# donc le diviseur qui garantit que le résultat reste dans [-1, 1] au lieu de
# pouvoir valoir 1,00003 sur un échantillon à pleine échelle négative.
INT16_SCALE = 32768.0


def wav_bytes(x: np.ndarray, sample_rate: int) -> bytes:
    """Sérialise en WAV 16 bits, en mémoire.

    Utilisé quand SAVE_REJECTED=true, et par le dump de diagnostic. On écrit un
    WAV et non un MP3, volontairement : le but est de régler le seuil, et un
    ré-encodage lossy fausserait l'analyse qu'on cherche justement à faire.

    En mémoire et non dans un fichier temporaire : l'appelant écrit le résultat
    par `storage.media.write_bytes`, qui est atomique. Un seul chemin
    d'écriture sur disque dans tout le service, donc un seul endroit où se
    poser la question de l'atomicité.

    L'en-tête produit fait EXACTEMENT 44 octets pour du PCM 16 bits mono ou
    stéréo — `WavSpool` s'appuie dessus pour écrire un WAV au fil de l'eau.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(float32_to_pcm16(x))
    return buf.getvalue()


def read_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """WAV → int16 mono, à la fréquence NATIVE.

    On ne rééchantillonne surtout pas ici : envoyer du 48 kHz que le serveur
    doit ramener à 16 kHz, c'est précisément le chemin qu'on veut tester (G4).

    Lève `ValueError` sur une largeur non gérée et non `SystemExit` : c'est un
    module de bibliothèque, c'est à l'appelant en ligne de commande de décider
    comment le dire à l'utilisateur.
    """
    with wave.open(str(path), "rb") as w:
        channels, width, rate, frames = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"{width * 8} bits non géré — WAV 16 bits attendu")

    data = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    if channels > 1:
        # En float AVANT la moyenne : .mean() sur des int16 tronque.
        data = data.astype(np.float32).mean(axis=1)
        data = np.clip(data, -32768, 32767).astype("<i2")
    return data.reshape(-1), rate


def read_wav_float32(path: str | Path) -> tuple[np.ndarray, int]:
    """WAV → float32 mono dans [-1, 1], à la fréquence native.

    Accepte 8, 16 et 32 bits : un fichier de terrain vient d'un outil qu'on n'a
    pas choisi, et refuser un WAV 24 bits pour une histoire de largeur serait
    une raison de plus de ne pas calibrer le seuil.
    """
    with wave.open(str(path), "rb") as w:
        channels, width, sr, frames = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        raw = w.readframes(frames)

    if width == 1:
        x = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / INT16_SCALE
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"largeur d'échantillon non gérée : {width * 8} bits")

    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, sr
