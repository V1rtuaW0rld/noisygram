"""Le projet : ce que cette installation cherche à compter.

Une seule ligne en base (`projet_config`), parce qu'une installation compte une
chose à la fois.

**`classes` est la seule sortie qui compte.** Les trois entrées de la modale —
nommer, uploader un son de référence, ou ne rien dire et curer le best-of —
produisent toutes la même chose : une liste de noms de classes YAMNet. Le nom et
le terme ne sont là que pour l'humain ; ils ne décident de rien.

C'est ce qui rend le réglage auditable : à tout moment on peut lire ce que
l'appli surveille, et le contredire.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import db

log = logging.getLogger(__name__)


def normaliser(classes: Any) -> list[str]:
    """Nettoie une liste de classes venue de la base ou de l'interface.

    Dédupliqué et sans blancs : la même classe écrite deux fois ne changerait
    pas le score, mais ferait croire à un groupe plus large qu'il n'est.
    """
    if isinstance(classes, str):
        # ⚠️ asyncpg rend une colonne JSONB sous forme de CHAÎNE — le dépôt le
        # sait déjà ailleurs (`api/events.py` fait `json.loads` sur
        # `top_classes`). Un `split(',')` sur cette chaîne découperait la
        # SYNTAXE du JSON, pas les valeurs : on obtenait
        # `['["Snoring"', '"Snort"', '"Wheeze"]']`, une seule classe nommée
        # d'après le texte du tableau. D'où le json.loads d'abord.
        try:
            classes = json.loads(classes)
        except (ValueError, TypeError):
            classes = classes.split(",")
    if not isinstance(classes, (list, tuple)):
        return []
    vues: list[str] = []
    for c in classes:
        nom = str(c).strip()
        if nom and nom not in vues:
            vues.append(nom)
    return vues


async def lire_hors_service() -> tuple[list[str] | None, dict | None]:
    """Pour les outils en ligne de commande (`python -m app.tools.…`).

    Ils n'ont pas de lifespan, donc pas de pool : on en ouvre un le temps de la
    lecture. Sans ça, un outil de diagnostic afficherait le groupe PAR DÉFAUT
    pendant que le service en surveille un autre — il mesurerait autre chose que
    ce qui tourne, et il mentirait.
    """
    from .config import settings

    await db.connect(settings.database_url)
    try:
        return await lire()
    finally:
        await db.disconnect()


async def lire() -> tuple[list[str] | None, dict | None]:
    """(classes à surveiller, ligne brute). `None` = aucune config posée.

    On ne connaît PAS le défaut ici, et c'est délibéré : le défaut vit dans le
    classifieur, à côté de `NOISY_CLASS_NAMES`. Sinon ce module — et tout ce qui
    l'importe — devrait connaître le groupe canin, ce qui recreuserait le
    couplage qu'on est en train d'enlever.
    """
    try:
        ligne = await db.fetchrow(
            "SELECT nom, terme, classes FROM projet_config WHERE id = 1"
        )
    except Exception as exc:  # noqa: BLE001
        # Ne jamais empêcher le service de démarrer pour un réglage : le défaut
        # vaut mieux qu'une capture à l'arrêt.
        log.warning("config projet illisible (%r) — défaut appliqué", exc)
        return None, None

    if not ligne:
        return None, None

    classes = normaliser(ligne["classes"])
    if not classes:
        log.warning("config projet sans aucune classe — défaut appliqué")
        return None, None
    return classes, {"nom": ligne["nom"], "terme": ligne["terme"]}


async def ecrire(nom: str | None, terme: str | None, classes: Any) -> list[str]:
    """Enregistre le projet. Refuse une liste vide.

    Un projet sans classe ne surveillerait rien : le service classerait tout à
    zéro et refuserait chaque épisode, en silence. C'est exactement le genre de
    panne qui ressemble à « le poste n'envoie rien ».
    """
    propres = normaliser(classes)
    if not propres:
        raise ValueError("un projet sans aucune classe ne surveillerait rien")

    await db.execute(
        """
        INSERT INTO projet_config (id, nom, terme, classes, updated_at)
        VALUES (1, $1, $2, $3, now())
        ON CONFLICT (id) DO UPDATE
           SET nom        = EXCLUDED.nom,
               terme      = EXCLUDED.terme,
               classes    = EXCLUDED.classes,
               updated_at = now()
        """,
        (nom or "").strip() or None,
        (terme or "").strip() or None,
        propres,
    )
    log.info("projet enregistré : %s → %s", nom or terme or "(sans nom)", propres)
    return propres
