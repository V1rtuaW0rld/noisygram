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
instantané et ne change rien à ce qui est compté ; l'*activer* change le groupe
de classes que la capture surveille.

Les deux sont immédiats. L'`Interpreter` LiteRT n'est pas thread-safe, mais le
groupe surveillé n'est PAS gravé dedans : c'est une sélection de colonnes dans
ses sorties, plus un seuil. L'échanger ne demande donc aucun rechargement, et
la capture suit d'elle-même au prochain `hello` ou au prochain ping du poste de
terrain — au plus 15 s (voir `app/surveillance.py`). Cette API a annoncé le
contraire pendant un temps, et a fait redémarrer des captures pour rien.
"""

import asyncio
import json
import logging
import tempfile
import urllib.error
import urllib.request
from typing import Any

import numpy as np
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from .. import dictionnaire, projet
from ..audio.resample import resample_to_16k
from ..audio.wav import read_wav_float32
from ..classifier.yamnet_litert import NOISY_CLASS_NAMES, load_class_names
from ..config import settings

log = logging.getLogger(__name__)

# Un extrait de quelques secondes suffit, et on refuse au-delà : le corps passe
# par la mémoire du conteneur, et personne n'a besoin d'envoyer un album.
TAILLE_MAX_OCTETS = 8 * 1024 * 1024

router = APIRouter(prefix="/api/projets", tags=["projets"])


class ProjetCree(BaseModel):
    nom: str | None = None
    terme: str | None = None
    classes: list[str]
    seuil: float | None = None


class ProjetRenomme(BaseModel):
    nom: str
    terme: str | None = None


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
        # [{nom, fr}] — la forme que consomme la liste à cocher. Le nom du
        # modèle reste en anglais : c'est ce que YAMNet comprend. Le français
        # n'est qu'une porte d'entrée, affichée à côté.
        "classes": dictionnaire.enrichir(propositions[0]["classes"]) if propositions else [],
    }


def _extrait_local(classifier, corps: bytes) -> dict[str, Any]:
    """YAMNet sur un court extrait, et les classes qui se sont manifestées.

    Bloquant (écriture disque + inférence) : à appeler via `asyncio.to_thread`.

    ⚠️ On rend le **MAX sur les fenêtres** pour chaque classe, pas la moyenne.
    Une classe qui se manifeste une demi-seconde dans un extrait de trois
    secondes est une moyenne basse mais un maximum franc — et c'est ce moment-là
    qui compte. C'est la même règle que le score principal.
    """
    if len(corps) > TAILLE_MAX_OCTETS:
        raise HTTPException(413, "extrait trop volumineux (8 Mo au maximum)")
    if not corps:
        raise HTTPException(400, "extrait vide")

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(corps)
        f.flush()
        try:
            x, sr = read_wav_float32(f.name)
        except Exception as exc:  # noqa: BLE001
            # Message utile plutôt que 500 : le navigateur peut n'avoir envoyé
            # qu'un en-tête, ou un format que le serveur ne décode pas.
            raise HTTPException(
                400,
                f"WAV illisible ({exc}) — le serveur n'accepte que du PCM "
                "(pas de MP3) ; la conversion se fait dans le navigateur",
            ) from exc

    x16 = resample_to_16k(x, sr)
    # Une fenêtre fait 15 600 échantillons : en dessous, le modèle complète par
    # des zéros et noterait surtout du silence.
    if x16.size < 15600:
        raise HTTPException(
            400, "extrait trop court : il faut au moins une seconde de son"
        )

    matrice = classifier.score_matrix(x16)
    noms = load_class_names(settings.class_map_path)
    par_classe = matrice.max(axis=0)
    order = np.argsort(par_classe)[::-1][:12]
    return {
        "classes": [
            {
                "nom": noms[int(i)],
                # Le français, quand on le connaît. Toutes les classes rendues
                # par le modèle n'y sont pas : on affiche alors l'anglais plutôt
                # que rien.
                "fr": dictionnaire.libelle(noms[int(i)]),
                "index": int(i),
                "score": round(float(par_classe[int(i)]), 4),
            }
            for i in order
        ],
        "duree_s": round(x16.size / 16000, 2),
        "windows": int(matrice.shape[0]),
    }


def _relais_extrait(corps: bytes) -> dict[str, Any]:
    """Demande l'analyse au processus capture, qui porte le modèle.

    Même idiome que `ondemand._relais_vers_capture` : l'admin sert la page mais
    n'a pas YAMNet, donc il relaie plutôt que de dupliquer le modèle.
    """
    base = settings.capture_url.rstrip("/")
    if not base:
        raise HTTPException(
            503,
            "aucun classifieur ici, et CAPTURE_URL n'est pas défini pour le relayer",
        )
    req = urllib.request.Request(
        base + "/api/projets/extrait",
        data=corps,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/json",
            "User-Agent": "noisygram/admin",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.analyze_remote_timeout_s) as rep:
            return json.loads(rep.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # On relaie le code ET le message : un 400 « WAV illisible » de capture
        # doit se lire comme un 400 ici, pas comme une panne de l'admin.
        #
        # ⚠️ Le corps d'erreur de capture est lui-même du JSON FastAPI
        # (`{"detail": …}`). Sans le déballer, l'interface afficherait du JSON
        # échappé à l'intérieur de JSON — illisible au moment précis où le
        # message doit aider.
        brut = exc.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(brut).get("detail", brut)
        except (ValueError, AttributeError):
            detail = brut
        raise HTTPException(exc.code, detail) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"capture injoignable : {exc}") from exc


@router.post("/extrait")
async def extrait(
    request: Request,
    corps: bytes = Body(..., media_type="application/octet-stream"),
) -> dict[str, Any]:
    """Les classes YAMNet qui répondent sur un court extrait de son.

    C'est la TROISIÈME voie de la modale : « voici le son que je veux compter ».
    Elle n'oblige pas l'utilisateur à connaître le vocabulaire du modèle — il
    enregistre dix secondes, et on lui rend les classes qui s'y manifestent.

    Le corps est du **WAV PCM**. Le MP3 est décodé dans le NAVIGATEUR avant
    l'envoi : le serveur n'embarque aucun décodeur, par choix (350 Mo de ffmpeg
    évités), et cette décision ne se revoit pas ici.
    """
    classifier = getattr(request.app.state, "classifier", None)
    if classifier is None or not classifier.is_ready():
        return await asyncio.to_thread(_relais_extrait, corps)
    return await asyncio.to_thread(_extrait_local, classifier, corps)


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
    """Rend ce projet actif — la capture le suivra d'elle-même."""
    actif = await projet.activer(projet_id)
    if actif is None:
        raise HTTPException(404, f"projet {projet_id} introuvable")
    return {
        "actif": actif,
        "message": (
            f"« {actif['nom']} » est maintenant le projet actif. La capture le "
            "suit sans redémarrage : au prochain ping du poste de terrain "
            "(15 s au plus), ou dès sa prochaine connexion."
        ),
    }


@router.get("/defaut")
async def defaut() -> dict[str, Any]:
    """Le groupe appliqué quand aucun projet n'est configuré."""
    return {"classes": list(NOISY_CLASS_NAMES)}


@router.get("/courant")
async def courant(request: Request) -> dict[str, Any]:
    """Ce que la CAPTURE surveille en ce moment.

    Demandé par la page du poste de terrain au chargement de la page : le titre
    doit dire ce qu'on alimente avant même qu'on clique sur Démarrer, et le
    WebSocket ne s'ouvre qu'à ce moment-là.

    ⚠️ Cet appel **relit le projet actif et l'applique** au lieu de se contenter
    de répondre. Ouvrir la page resynchronise donc la capture — c'est voulu :
    c'est le geste le moins cher pour rattraper un changement qu'aucun ping n'a
    encore vu, et il ne fait rien quand il n'y a rien à faire.
    """
    surveillance = getattr(request.app.state, "surveillance", None)
    if surveillance is None:
        # Rôle admin : aucun classifieur ici, donc rien à appliquer. On dit
        # quand même ce que la base porte — mais c'est la capture qui fait foi,
        # et cette page-là n'est servie que par elle.
        actif = await projet.actif()
        return {
            "projet": actif["nom"] if actif else None,
            "classes": actif["classes"] if actif else [],
            "seuil": actif["seuil"] if actif else None,
            "applique_ici": False,
        }
    await surveillance.suivre()
    applique = surveillance.applique
    return {
        "projet": surveillance.nom,
        "classes": applique["classes"] if applique else [],
        "seuil": applique["seuil"] if applique else None,
        "applique_ici": applique is not None,
    }


@router.patch("/{projet_id}")
async def renommer(projet_id: int, payload: ProjetRenomme) -> dict[str, Any]:
    """Renomme un projet. Ni les classes, ni le seuil ne bougent."""
    try:
        r = await projet.renommer(projet_id, payload.nom, payload.terme)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if r is None:
        raise HTTPException(404, f"projet {projet_id} introuvable")
    return {"projet": r}


@router.delete("/{projet_id}")
async def supprimer(projet_id: int) -> dict[str, Any]:
    """Supprime un projet et ses événements.

    Refuse le projet ACTIF et le dernier : dans les deux cas la capture se
    retrouverait sans cible, et ne le dirait qu'au redémarrage suivant.
    """
    try:
        emporte = await projet.supprimer(projet_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "supprime": emporte["nom"],
        "n_evenements": emporte["n_evenements"],
        "message": f"« {emporte['nom']} » supprimé, avec "
        f"{emporte['n_evenements']} événement(s).",
    }
