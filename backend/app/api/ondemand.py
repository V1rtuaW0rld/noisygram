"""Panneau des samples d'écoute — lister, réécouter, soumettre à YAMNet.

Monté sur le rôle CAPTURE uniquement, et ce n'est pas un choix de confort :
`export/` n'est monté que là (`docker-compose.yml`), et le classifieur non plus.
Le dashboard (rôle admin) ne pourrait ni lire ces fichiers ni les analyser.

Le dossier est un dossier de TRAVAIL, que l'utilisateur ouvre et manipule à la
main. Trois conséquences sur ce module :

  · on liste TOUT `.wav` du dossier, pas seulement les `_ondemand_` : ses
    dumps de debug sont de vraies prises de son du terrain, et pouvoir les
    réanalyser sans passer par la ligne de commande est exactement le besoin
    qui a fait écrire ce panneau ;
  · on ne supprime jamais rien ici (l'élagage est un réglage, pas une action
    d'écran), et l'index est indexé par CONTENU, donc un renommage à la main ne
    perd pas l'analyse ;
  · les réponses sont en `no-cache`, jamais en `immutable` : ces fichiers sont
    renommés et écrasés à la main, un cache d'un an figerait une version
    disparue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..analysis.timeline import build_timeline, noisy_scores
from ..audio.pcm import peak as niveau_pic
from ..audio.resample import TARGET_SR, resample_to_16k
from ..audio.wav import read_wav_float32
from ..classifier.remote_http import analyze_timeline_remote
from ..classifier.yamnet_litert import HOP_SAMPLES, load_class_names
from ..config import settings
from ..schemas import OndemandEntry, OndemandList, OndemandSample
from ..storage import media, ondemand
from ..ws.stream import compter_evenements
from .. import qc_client

log = logging.getLogger(__name__)

router = APIRouter(tags=["ondemand"])

# Un nom de fichier, et RIEN d'autre. `debugdump._assainir` assainit ce qu'on
# ÉCRIT ; ici on lit un nom qui vient de l'URL, donc la validation est
# obligatoire et porte sur les deux formes d'attaque : la traversée (`..`, `/`)
# et le fichier caché.
_NOM_VALIDE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.wav$")


def _niveau_du_fichier(chemin: Path) -> tuple[float | None, float] | None:
    """(dBFS, pic linéaire) d'un WAV, ou None si illisible.

    Une lecture de fichier entier pour un max : ~2 ms sur 1,9 Mo, ce qui est
    négligeable à côté du passage sous le verrou du classifieur. Et ça évite de
    faire porter au seul chemin d'analyse une information qui vaut justement
    pour les fichiers qu'on n'a pas envie de réanalyser.
    """
    try:
        x, sr = read_wav_float32(chemin)
    except (OSError, ValueError):
        return None
    if x.size == 0:
        return None
    p = niveau_pic(x)
    return (round(20 * math.log10(p), 1) if p > 0 else None), round(p, 6)


def _resoudre(name: str) -> Path:
    """Le chemin du sample, ou 404. Double barrière : le motif ET la résolution.

    Le motif suffit en théorie ; `is_relative_to` est là parce qu'une barrière
    unique sur un chemin venu de l'extérieur est le genre de chose qu'un
    `%2e%2e%2f` finit par contourner un jour.
    """
    racine = settings.ondemand_dir
    if not _NOM_VALIDE.match(name) or name.endswith(".part"):
        raise HTTPException(status_code=404, detail=f"sample inconnu : {name}")
    chemin = (racine / name).resolve()
    try:
        if not chemin.is_relative_to(racine.resolve()):
            raise HTTPException(status_code=404, detail=f"sample inconnu : {name}")
    except ValueError:
        raise HTTPException(status_code=404, detail=f"sample inconnu : {name}") from None
    if not chemin.is_file():
        raise HTTPException(status_code=404, detail=f"sample inconnu : {name}")
    return chemin


def _lister() -> list[Path]:
    racine = settings.ondemand_dir
    if not racine.is_dir():
        return []
    return sorted(
        (p for p in racine.glob("*.wav") if not p.name.endswith(".part") and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _entree(idx: ondemand.AnalysisIndex, chemin: Path, digest: str) -> tuple[OndemandEntry | None, bool]:
    """L'entrée d'index, et si elle est périmée.

    « Périmée » veut dire : analysée avec un autre seuil ou un autre modèle que
    ceux en service. Le seuil est provisoire par construction, donc une mesure
    qui ne dit pas avec quel point de fonctionnement elle a été prise n'est pas
    exploitable — c'est la règle du §17.2 appliquée à l'écran.
    """
    brut = idx.get(digest)
    if brut is None:
        return None, False
    entree = OndemandEntry.model_validate(brut)
    perimee = (entree.threshold is not None and entree.threshold != settings.noisy_threshold)
    return entree, perimee


@router.get("/ondemand", response_model=OndemandList)
async def liste() -> OndemandList:
    racine = settings.ondemand_dir
    chemins = await asyncio.to_thread(_lister)
    idx = ondemand.AnalysisIndex(racine)

    samples: list[OndemandSample] = []
    total = 0
    for chemin in chemins:
        try:
            stat = chemin.stat()
        except OSError:
            continue
        total += stat.st_size
        digest = await asyncio.to_thread(ondemand.sha256_file, chemin)
        entree, perimee = _entree(idx, chemin, digest)
        samples.append(
            OndemandSample(
                name=chemin.name,
                bytes=stat.st_size,
                mtime=int(stat.st_mtime * 1000),
                duration_ms=ondemand.wav_duration_ms(chemin),
                analysis=entree,
                stale=perimee,
            )
        )

    return OndemandList(
        dir=str(racine),
        samples=samples,
        total_bytes=total,
        # Le disque d'`export/`, pas celui de `media/` : ce sont deux montages
        # distincts, et interroger le mauvais volume donnerait un chiffre
        # rassurant sur un disque qui n'est pas celui qui se remplit.
        disk_free_bytes=media.free_bytes(racine),
        threshold=settings.noisy_threshold,
    )


@router.get("/ondemand/{name}")
async def detail(name: str) -> dict:
    chemin = _resoudre(name)
    idx = ondemand.AnalysisIndex(settings.ondemand_dir)
    digest = await asyncio.to_thread(ondemand.sha256_file, chemin)
    entree, perimee = _entree(idx, chemin, digest)
    return {
        "name": chemin.name,
        "bytes": chemin.stat().st_size,
        "duration_ms": ondemand.wav_duration_ms(chemin),
        "sha256": digest,
        "stale": perimee,
        "analysis": entree.model_dump() if entree else None,
    }


@router.post("/ondemand/{name}/analyser")
async def analyser(name: str, request: Request, relancer: bool = False) -> dict:
    """Analyse un sample et rend sa timeline : ce que le modèle croit entendre.

    **AUCUNE ÉCRITURE EN BASE.** C'est une analyse d'affichage, et c'est ce qui
    évite le double comptage : le noisygram est déjà alimenté tout seul par le
    YAMNet embarqué, à la fin de chaque écoute (`session.py`, `_finalize_listen`).
    Compter ici une seconde fois pour le même audio serait un doublon.

    Le seul effet de bord est la mise en cache dans `ondemand_index.json` —
    notre fichier, pas PostgreSQL — sous la clé sha256 déjà en place. `relancer`
    force un nouveau calcul au lieu de servir le cache.

    ⚠️ `score_matrix` tient le verrou du classifieur sur TOUTE sa boucle : ~0,5 s
    pour une minute, ~1,6 s pour trois. L'audio que l'opérateur écoute n'en
    souffre pas, parce qu'il ne passe pas par le classifieur — c'est la raison
    d'être du `publish()` placé avant le classement dans `session.py`. Seuls
    quelques scores de fenêtres peuvent être perdus. Si quelqu'un un jour fait
    transiter le PCM live par cette file, il réintroduira la panne ici.
    """
    chemin = _resoudre(name)
    digest = await asyncio.to_thread(ondemand.sha256_file, chemin)
    idx = ondemand.AnalysisIndex(settings.ondemand_dir)
    ancienne = idx.get(digest) or {}

    # Le cache d'abord : rouvrir la modale sur un fichier déjà analysé ne doit
    # pas recoûter un passage sous le verrou du classifieur.
    if not relancer and ancienne.get("timeline"):
        timeline = ancienne["timeline"]
        # Les entrées écrites AVANT que le niveau existe n'en ont pas. On le
        # mesure une fois et on enrichit l'entrée, plutôt que d'afficher « — »
        # jusqu'à ce que quelqu'un pense à cliquer « Relancer » — c'est
        # justement sur ces fichiers-là qu'on se demande pourquoi on n'entend
        # rien.
        if ancienne.get("peak_dbfs") is None:
            mesure = await asyncio.to_thread(_niveau_du_fichier, chemin)
            if mesure is not None:
                timeline["peak_dbfs"], timeline["peak"] = mesure
                enrichie = dict(ancienne)
                enrichie["timeline"] = timeline
                # Aussi à la racine : c'est là que la LISTE le lit.
                enrichie["peak_dbfs"], enrichie["peak"] = mesure
                await asyncio.to_thread(idx.put, digest, enrichie)
        return {
            "name": chemin.name,
            "stale": False,
            "cached": True,
            "analysed_at": ancienne.get("analysed_at"),
            "analysis": timeline,
        }

    try:
        x, sr = await asyncio.to_thread(read_wav_float32, chemin)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"lecture impossible : {exc}") from exc
    if x.size == 0:
        raise HTTPException(status_code=400, detail="fichier sans échantillon")

    x16 = resample_to_16k(x, sr)
    seuil = settings.noisy_threshold
    maintenant = datetime.now(timezone.utc)

    if settings.analyze_backend == "remote":
        timeline = await _timeline_distante(chemin)
        # Une analyse distante ne rend pas les scores par fenêtre : on garde ce
        # qu'elle donne et on laisse les champs locaux tels quels plutôt que de
        # les inventer.
        entree = dict(ancienne)
        entree.update(
            {
                "name": chemin.name,
                "bytes": chemin.stat().st_size,
                "origine": "panneau",
                "timeline": timeline,
                "timeline_source": "remote",
                "analysed_at": maintenant.isoformat(timespec="seconds"),
            }
        )
        await asyncio.to_thread(idx.put, digest, entree)
        return {"name": chemin.name, "stale": False, "cached": False,
                "analysed_at": entree["analysed_at"], "analysis": timeline}

    classifier = getattr(request.app.state, "classifier", None)
    if classifier is None or not classifier.is_ready():
        # Ce processus n'a PAS le modèle — c'est le rôle admin, où il n'est
        # délibérément pas chargé. On demande donc l'analyse à capture, qui
        # l'a, et on relaie sa réponse telle quelle.
        #
        # Le fichier existe des deux côtés : `./export` est monté sur les deux
        # services. Seul capture en écrit, mais les deux le lisent.
        return await asyncio.to_thread(_relais_vers_capture, chemin.name, relancer)

    noms = load_class_names(settings.class_map_path)

    # UN SEUL passage, et il donne les deux. `score_matrix` rend la matrice
    # (fenêtres × 521) sur EXACTEMENT la grille du chemin direct — `frame_signal`
    # est le même fenêtrage que `WindowScorer`. L'ancienne version faisait un
    # second balayage pour les scores principaux : deux passages sous le même verrou,
    # pour la même information.
    t0 = time.monotonic()
    matrix = await asyncio.to_thread(classifier.score_matrix, x16)
    scores_fenetres = noisy_scores(matrix, noms)
    timeline = build_timeline(
        matrix,
        noms,
        n_samples=x16.size,
        min_score=settings.analyze_timeline_min_score,
        noisy_threshold=seuil,
    )
    duree_calcul = time.monotonic() - t0

    # Le NIVEAU, mesuré sur l'audio déjà en mémoire — donc gratuit ici.
    #
    # C'est la mesure la plus utile de tout ce panneau, et de loin : un sample
    # dont le pic vaut -48 dBFS est INAUDIBLE, et se comporte exactement comme
    # un lecteur cassé. Sans ce chiffre, on cherche la panne dans le navigateur
    # au lieu de la chercher dans le microphone.
    #
    # Un pic nul ou quasi nul n'est PAS un micro débranché — un micro absent
    # donne des zéros francs. Un plancher de bruit à -50 dB, c'est un gain
    # d'entrée effondré ou une capsule obstruée.
    pic = niveau_pic(x16)
    pic_dbfs = round(20 * math.log10(pic), 1) if pic > 0 else None

    # Dans le dict de timeline ET à la racine de l'entrée : la modale lit
    # `analysis.peak_dbfs`, la liste lit `entry.peak_dbfs`. Deux lecteurs, deux
    # emplacements — les dupliquer coûte huit octets, les confondre coûterait
    # un « — » incompréhensible dans l'un des deux.
    timeline["peak"] = round(pic, 6)
    timeline["peak_dbfs"] = pic_dbfs

    offsets = [i * HOP_SAMPLES for i in range(len(scores_fenetres))]
    retenues = int((scores_fenetres >= seuil).sum())
    info = classifier.describe()

    entree = dict(ancienne)  # on FUSIONNE, on n'écrase pas
    entree.update(
        {
            "name": chemin.name,
            "bytes": chemin.stat().st_size,
            "origine": "panneau",
            "sample_rate": TARGET_SR,
            "duration_ms": round(x16.size / TARGET_SR * 1000),
            "threshold": seuil,
            "backend": info.get("backend"),
            "model": info.get("model"),
            "noisy_score": round(float(scores_fenetres.max()), 6) if scores_fenetres.size else None,
            "mean_noisy_score": round(float(scores_fenetres.mean()), 6) if scores_fenetres.size else None,
            "windows": int(scores_fenetres.size),
            "windows_retenues": retenues,
            "noisy_count": compter_evenements(list(scores_fenetres), offsets, seuil, TARGET_SR),
            "peak": round(pic, 6),
            "peak_dbfs": pic_dbfs,
            "scores": [
                {"offset_ms": round(o / TARGET_SR * 1000), "noisy": round(float(s), 6)}
                for o, s in zip(offsets, scores_fenetres)
            ],
            "timeline": timeline,
            "timeline_source": "local",
            "analysed_at": maintenant.isoformat(timespec="seconds"),
        }
    )

    try:
        dur_ms = round(x16.size / TARGET_SR * 1000)
        qc_sc, qc_val = await qc_client.evaluate_qc(str(chemin), dur_ms)
        entree["qc_score"] = qc_sc
        entree["qc_valid"] = qc_val
    except Exception:
        pass

    await asyncio.to_thread(idx.put, digest, entree)
    log.info(
        "sample %s analysé — %d fenêtre(s), %d retenue(s), score max %.3f, "
        "%d segment(s) en %.0f ms (seuil %.2f)",
        chemin.name,
        entree["windows"],
        retenues,
        entree["noisy_score"] or 0.0,
        len(timeline["timeline"]),
        duree_calcul * 1000,
        seuil,
    )
    return {
        "name": chemin.name,
        "stale": False,
        "cached": False,
        "analysed_at": entree["analysed_at"],
        "analysis": timeline,
    }


def _relais_vers_capture(nom: str, relancer: bool) -> dict:
    """Demande l'analyse au processus capture, qui porte le modèle.

    Bloquant (urllib) : à appeler via `asyncio.to_thread`, comme tout le reste
    de ce module.

    On ne réécrit RIEN en index ici : l'index vit dans le dossier partagé, et
    c'est capture qui l'a rempli en analysant. Le réécrire depuis l'admin
    reviendrait à deux écrivains sur le même fichier JSON — et le perdant
    écraserait l'autre.
    """
    base = settings.capture_url.rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="aucun classifieur ici, et CAPTURE_URL n'est pas défini pour le relayer",
        )
    url = f"{base}/api/ondemand/{urllib.parse.quote(nom)}/analyser"
    if relancer:
        url += "?relancer=true"

    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"Accept": "application/json", "User-Agent": "noisygram/admin"},
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.analyze_remote_timeout_s) as rep:
            return json.loads(rep.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # On relaie le code ET le message : un 404 « sample inconnu » de capture
        # doit se lire comme un 404 ici, pas comme une panne de l'admin.
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise HTTPException(status_code=exc.code, detail=f"capture : {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"capture injoignable ({base}) : {exc!r}"
        ) from exc


async def _timeline_distante(chemin: Path) -> dict:
    """Délègue à un service HTTP, quand `ANALYZE_BACKEND=remote`.

    Le service rend sa timeline dans SA forme ; on la normalise ici pour que la
    modale n'ait qu'une seule forme à connaître. Un `interval` comme
    « 7.2s à 8.16s » redevient des nombres — sans quoi le surlignage des
    fenêtres retenues et le tri devraient re-parser des chaînes côté navigateur.
    """
    if not settings.analyze_remote_url:
        raise HTTPException(
            status_code=503, detail="ANALYZE_BACKEND=remote exige ANALYZE_REMOTE_URL"
        )
    try:
        brut = await asyncio.to_thread(
            analyze_timeline_remote,
            settings.analyze_remote_url,
            chemin,
            settings.analyze_remote_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        # On nomme l'URL : sans elle, « analyse distante impossible » envoie
        # chercher un bug dans le panneau alors que le service est ailleurs.
        raise HTTPException(
            status_code=502, detail=f"analyse distante impossible ({settings.analyze_remote_url}) : {exc}"
        ) from exc

    timeline = []
    for item in brut.get("timeline", []):
        debut, fin = _bornes_intervalle(str(item.get("interval", "")))
        timeline.append(
            {
                "sound": item.get("sound"),
                "score": item.get("score"),
                "debut_s": debut,
                "fin_s": fin,
                "interval": item.get("interval"),
                "frames": None,
            }
        )
    return {
        "total_duration_sec": brut.get("total_duration_sec"),
        "frames": None,
        "source": "remote",
        "timeline": timeline,
        "noisy_frames": [],
        "noisy_max": None,
        "noisy_best": None,
    }


_BORNES = re.compile(r"([0-9.]+)\s*s\s*à\s*([0-9.]+)\s*s")


def _bornes_intervalle(intervalle: str) -> tuple[float | None, float | None]:
    """« 7.2s à 8.16s » → (7.2, 8.16). Rend (None, None) si illisible plutôt
    que de lever : une timeline distante mal formée doit s'afficher tant bien
    que mal, pas vider la modale."""
    m = _BORNES.search(intervalle)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))
