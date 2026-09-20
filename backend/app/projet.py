"""Les projets : ce que cette installation cherche à compter, et ses données.

Un projet porte **trois choses**, et les trois comptent :

- `classes` — le groupe de classes YAMNet dont le MAX fait le score principal.
  C'est la SEULE sortie qui décide ; le nom et le terme ne sont que pour
  l'humain ;
- `seuil` — le niveau au-dessus duquel une fenêtre est retenue. **Il appartient
  au projet** : 0,25 a été calibré sur des aboiements et ne veut rien dire pour
  une tronçonneuse. Un seuil global ferait accepter ou refuser n'importe quoi au
  premier changement de projet, sans que rien ne le signale ;
- l'appartenance des données — `events.projet_id` et `qc_snippets.projet_id`.

⚠️ **UN SEUL projet est actif à la fois.** C'est ce que la capture surveille, et
le charger demande un redémarrage : l'`Interpreter` LiteRT n'est pas
thread-safe. Consulter un autre projet est en revanche instantané — c'est une
simple bascule de vue, et c'est délibérément séparé de l'activation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

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


def _ligne(r: Any) -> dict:
    return {
        "id": r["id"],
        "nom": r["nom"],
        "terme": r["terme"],
        "classes": normaliser(r["classes"]),
        "seuil": r["seuil"],
        "actif": bool(r["actif"]),
    }


async def actif() -> dict | None:
    """Le projet actif, ou None si aucun n'est défini.

    On ne connaît PAS de défaut ici, et c'est délibéré : il vit dans le
    classifieur, à côté de `NOISY_CLASS_NAMES`. Sinon ce module — et tout ce qui
    l'importe — devrait connaître le groupe canin.
    """
    try:
        r = await db.fetchrow(
            "SELECT id, nom, terme, classes, seuil, actif "
            "FROM projets WHERE actif LIMIT 1"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("projet actif illisible (%r)", exc)
        return None
    return _ligne(r) if r else None


async def lister() -> list[dict]:
    """Tous les projets, l'actif d'abord, puis par ancienneté."""
    rows = await db.fetch(
        "SELECT id, nom, terme, classes, seuil, actif "
        "FROM projets ORDER BY actif DESC, id ASC"
    )
    return [_ligne(r) for r in rows]


async def creer(
    nom: str | None,
    terme: str | None,
    classes: Any,
    seuil: float | None = None,
) -> dict:
    """Crée un projet, INACTIF par défaut.

    Un projet neuf ne prend jamais la main tout seul : l'activer redémarre la
    capture, c'est donc un geste explicite.
    """
    propres = normaliser(classes)
    if not propres:
        raise ValueError("un projet sans aucune classe ne surveillerait rien")

    nom_propre = (nom or "").strip() or f"projet {len(await lister()) + 1}"
    r = await db.fetchrow(
        """
        INSERT INTO projets (nom, terme, classes, seuil, actif)
        VALUES ($1, $2, $3, $4, FALSE)
        RETURNING id, nom, terme, classes, seuil, actif
        """,
        nom_propre,
        (terme or "").strip() or None,
        propres,
        seuil,
    )
    log.info("projet créé : %s → %s", nom_propre, propres)
    return _ligne(r)


async def activer(projet_id: int) -> dict | None:
    """Rend un projet actif, et lui seul. L'index partiel le garantit."""
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE projets SET actif = FALSE WHERE actif")
            r = await conn.fetchrow(
                "UPDATE projets SET actif = TRUE, updated_at = now() "
                "WHERE id = $1 RETURNING id, nom, terme, classes, seuil, actif",
                projet_id,
            )
    if r:
        log.info("projet actif : %s", r["nom"])
    return _ligne(r) if r else None


async def definir_seuil(projet_id: int, seuil: float) -> None:
    if not 0.0 <= seuil <= 1.0:
        raise ValueError(f"seuil hors de [0,1] : {seuil}")
    await db.execute(
        "UPDATE projets SET seuil = $1, updated_at = now() WHERE id = $2",
        seuil,
        projet_id,
    )


async def garantir_projet(
    classes_defaut: Sequence[str], seuil_defaut: float
) -> dict | None:
    """Rend la base cohérente. Idempotent, appelé au démarrage.

    Trois choses, dans cet ordre :

    1. **Aucun projet ?** On en crée un, actif, avec le défaut du classifieur.
       C'est ce qui rend la migration 9 sans effet visible sur une installation
       qui tournait déjà.
    2. **Un seuil NULL ?** On le remplit. La migration ne pouvait pas le faire :
       elle est sans paramètre et ne connaît pas NOISY_THRESHOLD.
    3. **Des données orphelines ?** Les événements et les extraits antérieurs à
       la migration n'ont pas de projet. On les rattache à l'actif — sans quoi
       ils disparaîtraient de tous les écrans, en silence.

    ⚠️ Le rattrapage ne devine pas : tout ce qui est orphelin va au projet
    ACTIF. Sur une installation qui n'a jamais eu qu'un projet, c'est exact.
    """
    projets = await lister()
    if not projets:
        projet = await creer("aboiement", "bark", classes_defaut, seuil_defaut)
        await activer(projet["id"])
        projets = await lister()
        log.info("projet initial créé : aboiement → %s", list(classes_defaut))

    actif_courant = next((p for p in projets if p["actif"]), None)
    if actif_courant is None:
        # Aucun actif alors que des projets existent : on prend le plus ancien
        # plutôt que de laisser la capture sans cible.
        actif_courant = await activer(projets[0]["id"])

    remplis = await db.execute(
        "UPDATE projets SET seuil = $1 WHERE seuil IS NULL", seuil_defaut
    )
    if remplis and not remplis.endswith(" 0"):
        log.info("seuil par défaut appliqué aux projets non calibrés")

    for table in ("events", "qc_snippets"):
        n = await db.execute(
            f"UPDATE {table} SET projet_id = $1 WHERE projet_id IS NULL",  # noqa: S608
            actif_courant["id"],
        )
        if n and not n.endswith(" 0"):
            log.info("%s : lignes antérieures rattachées à « %s »", table, actif_courant["nom"])

    return actif_courant


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
        projet = await actif()
        return (projet["classes"] if projet else None), projet
    finally:
        await db.disconnect()
