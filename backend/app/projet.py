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
    """Tous les projets, l'actif d'abord, puis par ancienneté.

    `n_evenements` est joint parce que la modale doit pouvoir DIRE ce qu'une
    suppression emporte. Un bouton poubelle qui ne dit pas ce qu'il détruit est
    un piège, pas une fonctionnalité.
    """
    rows = await db.fetch(
        "SELECT id, nom, terme, classes, seuil, actif "
        "FROM projets ORDER BY actif DESC, id ASC"
    )
    projets = [_ligne(r) for r in rows]
    for p in projets:
        # `projet=p['id']` est INDISPENSABLE : la politique RLS ne rend que les
        # événements du projet courant, et le contexte ne suit pas tout seul.
        p["n_evenements"] = int(
            await db.fetchval(
                "SELECT count(*) FROM events WHERE projet_id = $1",
                p["id"],
                projet=p["id"],
            )
            or 0
        )
    return projets


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
        # ⚠️ `json.dumps` ET le cast `::jsonb`, les deux. asyncpg envoie le
        # paramètre en TEXTE et n'encode pas les objets Python lui-même : passer
        # la liste directement lève « expected str, got list ». C'est le motif
        # déjà en place dans `qc_client.save_grid`.
        """
        INSERT INTO projets (nom, terme, classes, seuil, actif)
        VALUES ($1, $2, $3::jsonb, $4, FALSE)
        RETURNING id, nom, terme, classes, seuil, actif
        """,
        nom_propre,
        (terme or "").strip() or None,
        json.dumps(propres),
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


async def renommer(
    projet_id: int, nom: str | None, terme: str | None = None
) -> dict | None:
    """Renomme un projet. Le NOM seul change : ni les classes, ni le seuil.

    Renommer ne doit pas toucher à ce qui décide — c'est le groupe de classes
    qui définit la cible, le nom n'est que pour l'humain.
    """
    nom_propre = (nom or "").strip()
    if not nom_propre:
        raise ValueError("un projet doit garder un nom")
    r = await db.fetchrow(
        "UPDATE projets SET nom = $1, terme = coalesce($2, terme), updated_at = now() "
        "WHERE id = $3 RETURNING id, nom, terme, classes, seuil, actif",
        nom_propre,
        (terme or "").strip() or None,
        projet_id,
    )
    return _ligne(r) if r else None


async def supprimer(projet_id: int) -> dict:
    """Supprime un projet ET ses événements. Rend ce qui a été emporté.

    ⚠️ Deux refus délibérés, et ils ne sont pas de la timidité :

    - **le projet ACTIF** : le supprimer laisserait la capture sans cible, et
      elle refuserait de démarrer au prochain redémarrage — une panne qui
      n'apparaîtrait que des heures plus tard ;
    - **le dernier projet** : même conséquence, immédiate.

    Les événements partent avec, en une seule transaction : un projet sans ses
    données et des données sans leur projet seraient tous deux incohérents.
    """
    projets = await lister()
    cible = next((p for p in projets if p["id"] == projet_id), None)
    if cible is None:
        raise ValueError(f"projet {projet_id} introuvable")
    if cible["actif"]:
        raise ValueError(
            "ce projet est celui que la capture surveille : active un autre "
            "projet avant de le supprimer"
        )
    if len(projets) <= 1:
        raise ValueError("c'est le dernier projet : il n'y aurait plus rien à compter")

    n = cible["n_evenements"]
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            # Le contexte, sinon la politique RLS ne laisserait pas voir les
            # lignes à supprimer — et elles resteraient orphelines.
            await conn.execute(
                "SELECT set_config('app.projet_id', $1, true)", str(projet_id)
            )
            await conn.execute("DELETE FROM qc_snippets WHERE projet_id = $1", projet_id)
            await conn.execute("DELETE FROM events WHERE projet_id = $1", projet_id)
            await conn.execute("DELETE FROM projets WHERE id = $1", projet_id)

    log.info("projet supprimé : « %s » et %d événement(s)", cible["nom"], n)
    return {"nom": cible["nom"], "n_evenements": n}


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
