"""YAMNet derrière LiteRT — backend par défaut.

Pourquoi pas Ollama (§2.1) : Ollama v0.20 sait transcrire de la parole, il
n'expose AUCUN modèle de classification d'événements sonores. Envoyer un
spectrogramme à un VLM serait lent, cher et non fiable. YAMNet a les classes
`Dog` et `Bark` nativement.

Pourquoi pas TensorFlow (§3.3) : `tflite-runtime` est une impasse (dernier
wheel x86_64 en cp39), et TensorFlow pèse 3,2 Go contre 21 Mo pour
`ai-edge-litert`. La machine est partagée avec un autre projet.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .base import ClassificationResult, ClassifierBackend

log = logging.getLogger(__name__)

# Le .tflite est FIGÉ à 15 600 échantillons (0,975 s @ 16 kHz), contrairement à
# la version TF qui fenêtre en interne. On réplique donc le hop natif de 0,48 s
# nous-mêmes (§5.3).
WINDOW_SAMPLES = 15_600
HOP_SAMPLES = 7_800
TOP_K = 5

# Le groupe canin : toutes les classes YAMNet qui désignent un chien ou une
# vocalise canine. Le score principal est le MAX sur ce groupe.
#
# « Whimper (dog) » porte la précision entre parenthèses dans le class map :
# c'est le libellé exact, et il évite d'attraper un « Whimper » humain s'il
# en existait un.
DOG_CLASS_NAMES = (
    "Dog",
    "Bark",
    "Yip",
    "Howl",
    "Bow-wow",
    "Growling",
    "Whimper (dog)",
)

# Indices de référence, utilisés UNIQUEMENT pour avertir. Les vrais sont
# résolus par nom au chargement : un class map différent doit continuer à
# fonctionner, pas planter.
EXPECTED_BARK_INDEX = 70
EXPECTED_DOG_INDEX = 69

PEAK_NORMALIZE_FLOOR = 0.1          # on ne touche à rien au-dessus
PEAK_NORMALIZE_TARGET = 0.5
PEAK_NORMALIZE_MAX_DB = 12.0        # plafond : un clip quasi silencieux ne
                                    # doit pas être amplifié en faux positif


def load_class_names(path: Path) -> list[str]:
    """Lit yamnet_class_map.csv (index,mid,display_name).

    Les noms sont entre guillemets et peuvent contenir des virgules : le
    module csv est obligatoire ici, un split(',') casserait.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"class map vide : {path}")
    names: list[str] = []
    for row in sorted(rows, key=lambda r: int(r["index"])):
        names.append(row["display_name"])
    return names


def frame_signal(x: np.ndarray) -> np.ndarray:
    """Découpe en fenêtres de 15 600, hop 7 800.

    Nombre de fenêtres = 1 + floor((n - 15600) / 7800) → 5 fenêtres pour 3 s
    à 16 kHz, exactement comme le `frame()` de TensorFlow, qui JETTE la
    dernière fenêtre partielle. Un signal plus court qu'une fenêtre produit
    UNE fenêtre complétée par des zéros (c'est le seul cas de complétion).
    """
    n = x.size
    if n < WINDOW_SAMPLES:
        padded = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        padded[:n] = x
        return padded.reshape(1, -1)

    starts = range(0, n - WINDOW_SAMPLES + 1, HOP_SAMPLES)
    return np.stack([x[s : s + WINDOW_SAMPLES] for s in starts])


class YamnetLitertBackend(ClassifierBackend):
    name = "yamnet-litert"

    def __init__(
        self,
        model_path: Path,
        class_map_path: Path,
        threshold: float = 0.35,
        peak_normalize: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.class_map_path = Path(class_map_path)
        self.threshold = threshold
        self.peak_normalize = peak_normalize

        self._interpreter = None
        self._input_index: int | None = None
        self._input_shape: tuple[int, ...] = ()
        self._output_index: int | None = None
        self._names: list[str] = []
        self._bark_index: int | None = None
        self._dog_index: int | None = None
        self._dog_indices: list[int] = []
        self._model_version: str | None = None

        # L'Interpreter LiteRT n'est PAS thread-safe : un seul thread à la
        # fois, et un pool d'UN thread pour ne jamais bloquer la boucle
        # d'événements pendant les ~30 ms d'inférence (§5.3).
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yamnet")

    # ---------------------------------------------------------------- load

    def load(self) -> None:
        with self._lock:
            if self._interpreter is not None:
                return

            if not self.model_path.exists():
                raise FileNotFoundError(
                    f"modèle absent : {self.model_path} — il est téléchargé au build "
                    "de l'image, pas au runtime"
                )
            if not self.class_map_path.exists():
                raise FileNotFoundError(f"class map absent : {self.class_map_path}")

            # Import paresseux : si le wheel LiteRT a un problème d'ABI NumPy,
            # l'erreur doit arriver ici avec un message utile, pas à l'import
            # du module, qui rendrait tout le service inimportable.
            from ai_edge_litert.interpreter import Interpreter

            self._names = load_class_names(self.class_map_path)
            self._model_version = self._hash_model()

            self._interpreter = Interpreter(
                model_path=str(self.model_path), num_threads=1
            )
            self._interpreter.allocate_tensors()

            in_details = self._interpreter.get_input_details()[0]
            out_details = self._interpreter.get_output_details()[0]
            self._input_index = int(in_details["index"])
            self._output_index = int(out_details["index"])

            # Le tenseur d'entrée de CE fichier (mediapipe, float32) est de
            # RANG 1 : forme (15600,), nom « waveform_binary ». La variante
            # tfhub-lite, elle, expose [1, 15600] sous le nom « audio_clip ».
            # On ne code donc aucune forme en dur : on lit celle du modèle et
            # on vérifie seulement qu'elle porte bien une fenêtre complète.
            in_shape = tuple(int(d) for d in in_details["shape"])
            if int(np.prod(in_shape)) != WINDOW_SAMPLES:
                raise ValueError(
                    f"le modèle attend {in_shape} ({int(np.prod(in_shape))} "
                    f"échantillons), or on fenêtre à {WINDOW_SAMPLES} — "
                    "mauvais fichier .tflite ?"
                )
            self._input_shape = in_shape
            log.info(
                "entrée=%s %s, sortie=%s %s",
                in_details["name"],
                in_shape,
                out_details["name"],
                tuple(int(d) for d in out_details["shape"]),
            )

            # Résolus PAR NOM, jamais codés en dur (§3.2). L'index 0 est
            # « Speech », pas « Animal » : une supposition ici décalerait tout.
            try:
                self._bark_index = self._names.index("Bark")
                self._dog_index = self._names.index("Dog")
            except ValueError as exc:
                raise ValueError(
                    "class map sans les classes 'Bark'/'Dog' — ce n'est pas le "
                    "yamnet_class_map.csv attendu"
                ) from exc

            if (self._bark_index, self._dog_index) != (
                EXPECTED_BARK_INDEX,
                EXPECTED_DOG_INDEX,
            ):
                log.warning(
                    "indices Bark/Dog inattendus : %d/%d (référence %d/%d) — "
                    "on continue avec ceux du fichier",
                    self._bark_index,
                    self._dog_index,
                    EXPECTED_BARK_INDEX,
                    EXPECTED_DOG_INDEX,
                )

            # Le groupe canin. Dog et Bark sont exigées juste au-dessus : la
            # liste n'est donc jamais vide. Les cinq autres sont un bonus —
            # leur absence réduit le rappel sans casser quoi que ce soit.
            for name in DOG_CLASS_NAMES:
                try:
                    self._dog_indices.append(self._names.index(name))
                except ValueError:
                    log.warning(
                        "classe canine absente du class map : %r — ignorée "
                        "(la détection se poursuit sur les autres)",
                        name,
                    )

            log.info(
                "YAMNet chargé : %d classes, groupe canin %s → indices %s, version=%s",
                len(self._names),
                list(DOG_CLASS_NAMES),
                self._dog_indices,
                self._model_version,
            )

    def _hash_model(self) -> str:
        # Empreinte courte : c'est ce qui part dans la colonne model_version et
        # qui permettra, dans six mois, de savoir quel binaire a produit une
        # ligne de la base.
        h = hashlib.sha256()
        with open(self.model_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return f"yamnet.tflite@{h.hexdigest()[:8]}"

    # ------------------------------------------------------------ propriétés

    def is_ready(self) -> bool:
        return self._interpreter is not None

    @property
    def bark_index(self) -> int:
        return self._bark_index

    @property
    def dog_index(self) -> int:
        return self._dog_index

    @property
    def model_version(self) -> str | None:
        return self._model_version

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "model": self.model_path.name,
            "dog_index": self._dog_index,
            "dog_classes": [[i, self._names[i]] for i in self._dog_indices],
            "bark_index": self._bark_index,
            "threshold": self.threshold,
            "window_samples": WINDOW_SAMPLES,
            "hop_samples": HOP_SAMPLES,
        }

    # ------------------------------------------------------------ inférence

    def _maybe_normalize(self, x: np.ndarray) -> np.ndarray:
        """Normalisation de niveau optionnelle (§5.3).

        Un micro extérieur derrière une bonnette sort souvent un pic à 0,02 et
        les scores s'effondrent. Plafonnée à +12 dB pour qu'un clip quasi
        silencieux ne devienne pas un faux positif.

        ATTENTION : ça DÉPLACE le point de fonctionnement. Changer ce flag
        invalide un seuil déjà réglé empiriquement (G20).
        """
        if not self.peak_normalize:
            return x
        p = float(np.max(np.abs(x))) if x.size else 0.0
        if 0.0 < p < PEAK_NORMALIZE_FLOOR:
            gain = min(PEAK_NORMALIZE_TARGET / p, 10.0 ** (PEAK_NORMALIZE_MAX_DB / 20.0))
            return x * gain
        return x

    def score_matrix(self, x_16k: np.ndarray) -> np.ndarray:
        """Matrice (fenêtres × 521) des scores bruts.

        Exposée pour l'outillage de calibration : régler un seuil sur un seul
        index sans voir la répartition des classes voisines, c'est deviner.
        """
        if self._interpreter is None:
            raise RuntimeError("modèle non chargé — appeler load()")
        x = np.asarray(x_16k, dtype=np.float32).reshape(-1)
        x = self._maybe_normalize(x)
        windows = frame_signal(x)
        out = np.empty((windows.shape[0], len(self._names)), dtype=np.float32)
        with self._lock:
            for i, w in enumerate(windows):
                self._interpreter.set_tensor(
                    self._input_index, w.reshape(self._input_shape)
                )
                self._interpreter.invoke()
                out[i] = self._interpreter.get_tensor(self._output_index).reshape(-1)
        return out

    def classify(self, x_16k: np.ndarray) -> ClassificationResult:
        if self._interpreter is None:
            raise RuntimeError("modèle non chargé — appeler load()")

        t0 = time.perf_counter()
        x = np.asarray(x_16k, dtype=np.float32).reshape(-1)
        if x.size == 0:
            raise ValueError("signal vide")
        x = self._maybe_normalize(x)

        windows = frame_signal(x)
        n_classes = len(self._names)
        scores = np.empty((windows.shape[0], n_classes), dtype=np.float32)

        # Le verrou couvre TOUTE la boucle, pas chaque invoke : sinon deux
        # threads pourraient entrelacer leurs set_tensor/invoke et se rendre
        # des scores mélangés, silencieusement.
        with self._lock:
            for i, w in enumerate(windows):
                self._interpreter.set_tensor(
                    self._input_index, w.reshape(self._input_shape)
                )
                self._interpreter.invoke()
                scores[i] = self._interpreter.get_tensor(self._output_index).reshape(-1)

        # MAX sur les fenêtres ET sur le groupe canin. C'est le score
        # principal, celui sur lequel porte le seuil.
        dog_per_window = scores[:, self._dog_indices].max(axis=1)
        best_window = int(np.argmax(dog_per_window))

        dog = float(dog_per_window.max())
        # Bark reste mesurée séparément, en diagnostic : c'est la colonne qui
        # permettra de vérifier après coup si le changement de critère était le
        # bon, sur des données réelles et non sur six segments.
        bark = float(scores[:, self._bark_index].max())
        mean_dog = float(dog_per_window.mean())

        # Le top-K est celui de la fenêtre qui a produit le score retenu : une
        # liste agrégée sur des fenêtres différentes ne décrirait rien de réel.
        order = np.argsort(scores[best_window])[::-1][:TOP_K]
        top = [
            (self._names[int(i)], int(i), float(scores[best_window][int(i)]))
            for i in order
        ]

        return ClassificationResult(
            dog_score=dog,
            bark_score=bark,
            mean_dog_score=mean_dog,
            top_classes=top,
            backend=self.name,
            model_version=self._model_version,
            windows=int(windows.shape[0]),
            processing_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def classify_async(self, x_16k: np.ndarray) -> ClassificationResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.classify, x_16k)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
