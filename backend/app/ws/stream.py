"""Épisodes streamés — spool, fenêtrage incrémental, décision.

Trois objets, chacun né d'un défaut précis :

  WindowScorer   reproduit `frame_signal` en INCRÉMENTAL, pour ne jamais tenir
                 l'épisode entier en mémoire ni garder le verrou du classifieur
                 pendant des secondes.
  EpisodeWriter  écrit le PCM au fil de l'eau dans un `.part`, pour que la
                 mémoire du serveur ne dépende pas de la longueur de l'épisode.
  StreamState    l'état d'un épisode en cours.

Pourquoi un flux et pas des clips recollés : deux clips déclenchés
indépendamment ne se touchent pas. Mesuré sur le terrain, il manque 9 à 52 ms
de son à chaque couture — le déclencheur a une gigue d'une trame et chaque clip
couvre exactement sa durée nominale. Un épisode de deux minutes aurait quarante
micro-coupures, et une pièce à conviction trouée n'en est plus une.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..classifier.yamnet_litert import HOP_SAMPLES, WINDOW_SAMPLES
from ..audio.pcm import pcm16_to_float32

log = logging.getLogger(__name__)


@dataclass
class Bilan:
    """Ce que `segment_result` attend d'un classifieur, reconstitué depuis les
    scores par fenêtre d'un épisode.

    Un épisode n'a pas « un » score : il en a un par fenêtre de 975 ms. Le
    score principal devient donc le MAXIMUM — la règle du groupe surveillé, celle
    qui décide depuis le début — et la moyenne, qui sur trois minutes cesse
    d'être du bruit, garde son sens de mesure du fond sonore.
    """

    noisy_score: float
    bark_score: float | None
    mean_noisy_score: float
    top_classes: list | None = None
    processing_ms: float = 0.0


class WindowScorer:
    """Fenêtre glissante incrémentale, identique à `frame_signal`.

    `frame_signal` découpe `range(0, n - WINDOW + 1, HOP)` : les fenêtres sont
    donc ancrées au PREMIER échantillon du signal. Ici le signal complet n'est
    jamais disponible — il arrive par morceaux — donc l'origine de la grille est
    mémorisée et transmise avec chaque fenêtre.

    ⚠️ Se tromper d'origine décale tous les scores d'un demi-hop, et RIEN ne le
    signale : les scores restent plausibles, seulement décalés dans le temps.
    C'est la panne la plus difficile à voir de tout le mécanisme, d'où le test
    qui compare cette classe à `frame_signal` sur des morceaux irréguliers.

    Le tampon est borné par WINDOW + HOP : la mémoire ne dépend pas de la
    longueur de l'épisode.

    UNE divergence assumée avec `frame_signal` : un signal plus court qu'une
    fenêtre y produit UNE fenêtre complétée par des zéros ; ici, aucune. Une
    fenêtre à 97 % de silence donnerait un score qui parle du remplissage, pas
    de l'audio — et le seul cas qui l'atteint est un épisode interrompu avant
    975 ms, qu'il vaut mieux écarter que classer sur du vide.
    """

    def __init__(self, window: int = WINDOW_SAMPLES, hop: int = HOP_SAMPLES) -> None:
        self.window = window
        self.hop = hop
        self._buf = np.empty(0, dtype=np.float32)
        self._origine = 0  # rang du premier échantillon du tampon dans l'épisode

    def feed(self, x: np.ndarray) -> list[tuple[int, np.ndarray]]:
        """Rend les fenêtres devenues complètes : (décalage en échantillons, signal)."""
        if x.size:
            self._buf = np.concatenate([self._buf, x]) if self._buf.size else x
        sorties: list[tuple[int, np.ndarray]] = []
        while self._buf.size >= self.window:
            sorties.append((self._origine, self._buf[: self.window].copy()))
            self._buf = self._buf[self.hop :]
            self._origine += self.hop
        return sorties

    @property
    def reste(self) -> int:
        """Échantillons en attente de complétion — jamais classés, par
        construction : c'est la queue de l'épisode, bornée par WINDOW."""
        return int(self._buf.size)


class EpisodeWriter:
    """Spool du PCM brut d'un épisode, sur disque, au fil de l'eau.

    On écrit le PCM et non le MP3 : l'encodage n'a lieu qu'à la fin, une seule
    fois, sur un tableau déjà rogné. Cela supprime deux problèmes d'un coup —
    l'`Encoder` de lameenc est *stateful*, et l'appeler par morceaux dégraderait
    le débit ; et la majorité des épisodes étant détruite, on n'encode pas trois
    minutes de MP3 pour jeter le fichier ensuite.

    Le fichier vit sous `media_dir/.tmp`, donc sur le MÊME volume que sa
    destination : `os.replace` reste atomique au moment de publier.
    """

    def __init__(self, media_dir: Path, relpath: str) -> None:
        self.path = media_dir / relpath
        self.samples = 0
        self._fh = None

    def write(self, data: bytes) -> None:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "wb")
        self._fh.write(data)
        self.samples += len(data) // 2

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def read_range(self, premier: int, dernier: int) -> np.ndarray:
        """Les échantillons [premier, dernier) en float32, en une seule lecture.

        On ne lit QUE la plage retenue : les trente secondes de silence terminal
        qui ont déclenché la fin de l'épisode ne sont jamais chargées.
        """
        self.close()
        debut = max(0, premier)
        fin = max(debut, dernier)
        with open(self.path, "rb") as fh:
            fh.seek(debut * 2)
            brut = fh.read((fin - debut) * 2)
        return pcm16_to_float32(brut, channels=1)

    def discard(self) -> None:
        """Efface le temporaire. Un épisode sans détection ne laisse RIEN."""
        self.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            log.warning("temporaire non supprimé : %s — %s", self.path, exc)


@dataclass
class StreamState:
    """Un épisode en cours de réception.

    Sert aussi à l'ÉCOUTE À LA DEMANDE. Le `writer` est alors un `WavSpool` et
    non un `EpisodeWriter` : les deux exposent `write()` et `read_range()` avec
    la même sémantique d'indices d'ÉCHANTILLONS, ce qui permet de partager le
    rognage et le stockage sans adaptateur. Ce qui diffère est la fin —
    `finalize()` publie un WAV, `discard()` efface — et c'est pour ça que les
    deux finalisations restent deux méthodes distinctes.
    """

    seq: int
    sample_rate: int
    pre_roll_samples: int
    received_at: datetime
    # EpisodeWriter (épisode) ou WavSpool (écoute). Les deux exposent write()
    # et read_range(premier, dernier) en échantillons.
    writer: object
    scorer: WindowScorer
    # Scores YAMNet, un par fenêtre, et le décalage de cette fenêtre dans
    # l'épisode. Les deux listes vont par paire et ne sont jamais triées.
    scores: list[float] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    barks: list[float] = field(default_factory=list)

    # File des fenêtres à classer, bornée. Pleine → on compte un `dropped`
    # plutôt que de bloquer la réception : un épisode dont on n'a pas pu
    # classer toutes les fenêtres reste exploitable, à condition de le DIRE.
    queue: object = None
    task: object = None
    watchdog: object = None
    dernier_progres: float = 0.0

    chunks: int = 0
    bytes_recus: int = 0
    samples: int = 0
    dropped: int = 0  # fenêtres perdues faute de place dans la file
    rms_vus: list[float] = field(default_factory=list)
    derniere_trame: float = 0.0  # monotonic
    dernier_bruyant: float = 0.0  # monotonic
    clos: bool = False

    @property
    def duree_ms(self) -> int:
        return round(self.samples / self.sample_rate * 1000)

    def premier_instant(self) -> datetime:
        """Instant serveur du PREMIER échantillon de l'épisode.

        `pre_roll_samples` est une DURÉE, pas un instant : elle ne dépend
        d'aucune horloge et ne peut pas dériver. C'est ce qui permet de dater
        l'épisode sans jamais faire confiance à l'horloge du vieux PC — la
        contrainte G6 du projet tient donc toujours, et mieux qu'avant : on
        date maintenant l'événement, plus le déclencheur.
        """
        return self.received_at - timedelta(
            milliseconds=self.pre_roll_samples / self.sample_rate * 1000
        )

    def bornes_retenues(self, seuil: float) -> tuple[int, int] | None:
        """L'intervalle [début, fin) à garder, en échantillons — ou None si
        aucune fenêtre n'a dépassé le seuil."""
        gardees = [i for i, s in enumerate(self.scores) if s >= seuil]
        if not gardees:
            return None
        debut = self.offsets[gardees[0]]
        fin = self.offsets[gardees[-1]] + self.scorer.window
        return debut, fin


def compter_evenements(
    scores: list[float],
    offsets: list[int],
    seuil: float,
    sample_rate: int,
    fusion_ms: int = 500,
) -> int:
    """Nombre de rafales distinctes, pas de fenêtres.

    Deux fenêtres voisines se recouvrent à 50 % (fenêtre 975 ms, hop 487 ms) :
    un seul événement en allume donc deux, parfois trois. On ne compte une
    nouvelle rafale que si le dépassement précédent date de plus de `fusion_ms`
    — converti en ÉCHANTILLONS, car les décalages sont en échantillons.

    C'est ce compteur qui permet aux graphiques de continuer à parler
    d'« événements » alors qu'une ligne de base est désormais un épisode.
    """
    fusion = int(fusion_ms * sample_rate / 1000)
    rafales = 0
    dernier: int | None = None
    for score, offset in zip(scores, offsets):
        if score < seuil:
            continue
        if dernier is None or (offset - dernier) > fusion:
            rafales += 1
        dernier = offset
    return rafales
