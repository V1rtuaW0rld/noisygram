"""Interface du classifieur.

Tout ce que le reste de l'application sait d'un classifieur passe par ici.
Changer de modèle (YAMNet → un pont GPU distant, ou un YAMNet v2) ne doit
toucher que les implémentations de cette interface.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ClassificationResult:
    noisy_score: float
    """SCORE PRINCIPAL : MAX, sur les fenêtres ET sur le groupe de classes
    surveillées (Dog, Bark, Yip, Howl, Bow-wow, Growling, Whimper), pas sur la
    seule classe « Bark ».

    Deux raisons, l'une mesurée, l'autre structurelle :

    • Mesurée — sur les enregistrements de référence du terrain, Bark plonge à
      0,262 là où Dog monte à 0,586, sur les MÊMES segments. Bark est le moins
      bon discriminateur du groupe surveillé : des événements réels et lointains
      marquent sur « Dog » bien plus que sur « Bark », entraînée sur des
      événements proches et isolés.
    • Structurelle — une source qui hurle marque sur Howl, pas sur Dog. Prendre
      le max du groupe ne peut qu'améliorer le rappel, et les sept classes
      étant toutes surveillées, la spécificité ne se dilue pas — contrairement à
      « Animal » (67), qui réagirait aux chats et aux oiseaux.

    MAX et non moyenne (§5.4) : un clip de 3 s contenant un événement de 0,5 s
    et 2,5 s de vent a un pic élevé et une moyenne basse ; moyenner le
    rejetterait."""

    bark_score: float | None
    """Diagnostic : la classe « Bark » seule, MAX sur les fenêtres. Conservée
    parce qu'elle reste la mesure la plus proche de la nuisance qu'on cherche à
    quantifier, et qu'elle permet de comparer les deux critères après coup."""

    mean_noisy_score: float
    """Stocké EN PLUS, pour le réglage ultérieur : c'est la colonne qui dira si
    0,35 était le bon seuil."""

    top_classes: list[tuple[str, int, float]] = field(default_factory=list)
    backend: str = "unknown"
    model_version: str | None = None
    windows: int = 0
    processing_ms: float = 0.0

    def top_dicts(self) -> list[list]:
        """Forme sérialisable en JSONB (listes, pas de tuples)."""
        return [[name, idx, round(score, 6)] for name, idx, score in self.top_classes]


class ClassifierBackend(abc.ABC):
    """Un classifieur charge un modèle, puis qualifie des extraits.

    Contrat de concurrence : `classify()` peut être appelé depuis n'importe
    quel thread, l'implémentation DOIT se protéger elle-même. YAMNet le fait
    avec un verrou, parce que l'Interpreter LiteRT n'est pas thread-safe.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def load(self) -> None:
        """Charge le modèle. Idempotent."""

    @abc.abstractmethod
    def is_ready(self) -> bool: ...

    @abc.abstractmethod
    def classify(self, x_16k) -> ClassificationResult:
        """x_16k : float32 mono à 16 kHz. Bloquant, thread-safe."""

    @abc.abstractmethod
    async def classify_async(self, x_16k) -> ClassificationResult:
        """Même chose, sans bloquer la boucle d'événements."""

    @abc.abstractmethod
    def describe(self) -> dict:
        """Métadonnées annoncées dans hello_ack."""

    def reconfigurer(self, classes_cibles: list[str], seuil: float) -> None:
        """Change le groupe surveillé et le seuil, SANS recharger le modèle.

        Un projet qui change n'est plus un redémarrage : le groupe surveillé
        n'est pas gravé dans le modèle, c'est une sélection parmi ses sorties.
        Changer de projet, c'est donc réécrire deux attributs — pas rappeler
        `load()`.

        Peut lever si le groupe demandé ne correspond pas à ce modèle. Dans ce
        cas l'appelant garde l'ancien groupe : appliquer à moitié donnerait une
        capture qui compte autre chose que ce qu'elle croit compter.

        ⚠️ Appelé pendant que `classify()` tourne dans un autre thread. Une
        implémentation qui dérive des indices doit les publier ATOMIQUEMENT
        (un seul rebind d'attribut), jamais les vider puis les remplir : une
        fenêtre où le groupe est vide ferait juger un segment par personne.
        """
        self.classes_cibles = tuple(classes_cibles)
        self.threshold = seuil

    def close(self) -> None:
        return None
