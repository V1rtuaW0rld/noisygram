"""API des projets : lister, créer, activer, et proposer des classes.

Les trois voies de la modale aboutissent toutes au MÊME objet — une liste de
noms de classes YAMNet :

- **nommer** : `GET /proposer?terme=…` interroge le dictionnaire, qui rend les
  classes candidates ; l'utilisateur les corrige puis crée ;
- **uploader** : le son de référence donne les classes qui répondent dessus
  (étape suivante — elle demande de rejouer YAMNet sur le fichier) ;
- **ne rien dire** : on crée avec un groupe par défaut, et le best-of se
  remplira ensuite par « + Réf ».

⚠️ **Voir et surveiller sont deux gestes distincts.** Consulter un projet est
instantané ; l'*activer* change ce que la capture surveille et **exige un
redémarrage** — l'`Interpreter` LiteRT n'est pas thread-safe. L'API le dit dans
sa réponse plutôt que de laisser croire à une bascule immédiate.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import dictionnaire, projet
from ..classifier.yamnet_litert import NOISY_CLASS_NAMES, load_class_names
from ..config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projets", tags=["projets"])


class ProjetCree(BaseModel):
    nom: str | None = None
    terme: str | None = None
    classes: list[str]
    seuil: float | None = None


def _noms_du_modele() -> set[str]:
    """Les 521 classes que le modèle sait nommer.

    Lire le CSV ne charge PAS YAMNet : le rôle admin n'a ni LiteRT ni
    l'Interpreter, et n'en a pas besoin pour valider une liste de noms.
    """
    try:
        return set(load_class_names(settings.class_map_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("class map illisible (%r) — validation ignorée", exc)
        return set()


@router.get("")
async def liste() -> dict[str, Any]:
    projets = await projet.lister()
    actif = next((p for p in projets if p["actif"]), None)
    return {"projets": projets, "actif": actif}


# ⚠️ AVANT toute route à paramètre : `/proposer` est littéral, et une route
# `/{id}` déclarée plus haut l'avalerait. Le dépôt a un test pour cette règle.
@router.get("/proposer")
async def proposer(terme: str) -> dict[str, Any]:
    """Les classes YAMNet qui se rapportent à un terme, en français ou anglais."""
    propositions = dictionnaire.proposer(terme)
    return {
        "terme": terme,
        "propositions": propositions,
        # Rendu tel quel pour que l'interface n'ait pas à connaître le
        # dictionnaire : la meilleure proposition est prête à être proposée.
        "classes": propositions[0]["classes"] if propositions else [],
    }


@router.post("")
async def creer(payload: ProjetCree) -> dict[str, Any]:
    """Crée un projet INACTIF. L'activer est un geste séparé, et il redémarre."""
    proposees = projet.normaliser(payload.classes)
    if not proposees:
        raise HTTPException(400, "un projet sans aucune classe ne surveillerait rien")

    connues = _noms_du_modele()
    if connues:
        inconnues = [c for c in proposees if c not in connues]
        if inconnues:
            # Refuser plutôt que d'accepter : une classe mal orthographiée ne
            # résout pas au chargement, le groupe devient silencieusement plus
            # étroit, et la détection rate sans le dire.
            raise HTTPException(
                400,
                f"classe(s) inconnue(s) du modèle : {inconnues} — "
                "vérifie l'orthographe exacte (le class map est en anglais)",
            )

    cree = await projet.creer(payload.nom, payload.terme, proposees, payload.seuil)
    return {
        "projet": cree,
        # Le dire franchement : un seuil repris d'un autre projet fait accepter
        # ou refuser n'importe quoi, et rien ne le signale.
        "conseil": (
            "Seuil repris du réglage global — il est calibré pour un autre son. "
            "Mesure-le avec `python -m app.tools.selftest` sur tes propres "
            "extraits avant de compter quoi que ce soit."
            if payload.seuil is None
            else "Seuil fourni."
        ),
    }


@router.post("/{projet_id}/activer")
async def activer(projet_id: int) -> dict[str, Any]:
    """Rend ce projet actif — ce que la CAPTURE surveillera après redémarrage."""
    actif = await projet.activer(projet_id)
    if actif is None:
        raise HTTPException(404, f"projet {projet_id} introuvable")
    return {
        "actif": actif,
        "redemarrage_requis": True,
        "message": (
            f"« {actif['nom']} » est maintenant le projet actif. "
            "La capture doit être redémarrée pour le surveiller : "
            "docker compose restart capture"
        ),
    }


@router.get("/defaut")
async def defaut() -> dict[str, Any]:
    """Le groupe appliqué quand aucun projet n'est configuré."""
    return {"classes": list(NOISY_CLASS_NAMES)}
