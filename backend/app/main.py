"""Point d'entrée de l'application.

Ordre d'initialisation, qui n'est pas négociable :

    1. journalisation
    2. dossier des médias
    3. pool PostgreSQL + DDL
    4. chargement du modèle
    5. routes, PUIS montages statiques

Les montages viennent en dernier parce que FastAPI apparie dans l'ordre
d'enregistrement : un `mount("/")` posé trop tôt avale silencieusement toutes
les routes d'API déclarées après lui (G16). Le symptôme est déroutant — les
routes existent, elles sont dans /docs, et elles renvoient 404.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import events, health, ondemand as ondemand_api, qc as qc_api, stats
from .classifier.factory import build_classifier
from .config import settings
from .logging_setup import setup_logging
from .storage import media, ondemand
from .ws import proxy as ws_proxy
from .ws import routes as ws_routes
from .ws.hub import ListenHub

setup_logging(settings.log_level)

log = logging.getLogger("aboigramme")

# Avant tout montage : StaticFiles refuse un dossier absent, et le montage est
# évalué à l'import, donc avant le lifespan.
media.ensure_root(settings.media_dir)
media.ensure_root(settings.media_dir / "rejected")
if settings.ondemand_enabled and (settings.sert_capture or settings.sert_admin):
    # Les deux rôles montent `/ondemand` depuis ce dossier : il doit exister à
    # l'import, sinon `StaticFiles` lève et le service ne démarre pas.
    media.ensure_root(settings.ondemand_dir)


class MarkdownAsText(StaticFiles):
    """Sert les procédures en `text/plain`.

    Sans ça, un `.md` part en `text/markdown`, que le navigateur TÉLÉCHARGE au
    lieu de l'afficher. Or ces procédures se lisent sur un téléphone, dehors,
    à côté de la machine — pas dans un éditeur après un téléchargement.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".md") and response.status_code == 200:
            response.headers["content-type"] = "text/plain; charset=utf-8"
        return response


class SampleStaticFiles(StaticFiles):
    """Sert les samples d'écoute pour la réécoute depuis le panneau.

    Deux différences avec `ImmutableStaticFiles`, et les deux sont délibérées :

    · **`no-cache`, pas `immutable`.** Le réflexe serait de recopier l'en-tête
      de `/media` — un MP3 nommé d'après son `event_id` ne change jamais. Ici
      c'est l'inverse : ces fichiers sont renommés et écrasés à la main, et un
      cache d'un an figerait durablement une version qui n'existe plus.
    · **Les `.part` sont en 404.** Le dossier est un dossier de travail monté
      depuis l'hôte : on y trouve les WAV en cours d'écriture, et un `.part` lu
      à mi-parcours est un WAV dont l'en-tête annonce zéro échantillon — le
      navigateur jouerait un silence de soixante secondes sans rien dire.
    """

    async def get_response(self, path: str, scope):
        morceaux = PurePosixPath(path).parts
        if any(p.startswith(".") or p.endswith(".part") for p in morceaux):
            return Response(status_code=404)
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response


class ImmutableStaticFiles(StaticFiles):
    """Sert les MP3 avec un cache immuable.

    Un MP3 est nommé d'après l'identifiant de son événement : son contenu ne
    change jamais, il ne peut que disparaître. Le navigateur peut donc le
    garder indéfiniment, ce qui compte pour un dashboard qu'on recharge
    souvent sur un vieux PC.
    """

    async def get_response(self, path: str, scope):
        # `/media` est monté sur TOUT `media_dir`, donc `rejected/` et `.tmp/`
        # compris — contrairement à ce que dit la docstring de
        # `rejected_relpath`. Un temporaire d'épisode (plusieurs mégaoctets de
        # PCM brut) serait donc servi à qui devine son nom, et le corpus de
        # rejets, qui existe pour régler le seuil, serait écoutable depuis
        # l'extérieur. Rien de tout ça ne doit sortir.
        morceaux = PurePosixPath(path).parts
        if any(p.startswith(".") or p == "rejected" for p in morceaux):
            return Response(status_code=404)

        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("démarrage — version %s, TZ %s", settings.server_version, settings.app_tz)

    # Le pool est créé ICI et pas à l'import : asyncpg lie ses connexions à la
    # boucle d'événements courante, et un pool créé à l'import casse le reload
    # de façon silencieuse (G14).
    await db.connect(settings.database_url)
    await db.run_ddl()
    await db.migrate()

    # Les temporaires d'épisodes abandonnés (processus tué en pleine écriture).
    # Ils pèsent maintenant plusieurs mégaoctets chacun : sans ce balayage, ils
    # s'accumulent en silence jusqu'au disque plein.
    oublies = media.sweep_spool(settings.media_dir)
    if oublies:
        log.warning("%d temporaire(s) d'épisode abandonné(s) supprimé(s)", oublies)

    # Les `.part` d'écoute : RÉCUPÉRÉS, pas supprimés. À cet instant aucune
    # écoute ne peut être en cours — elles vivent dans les sessions WebSocket,
    # qui n'existent pas encore — donc tout `.part` trouvé est orphelin, et
    # c'est la seule copie d'une prise de son réelle. À l'inverse de
    # `sweep_spool`, qui jette : un épisode non finalisé n'a pas été jugé.
    if settings.sert_capture and settings.ondemand_enabled:
        recuperes = ondemand.sweep(settings.ondemand_dir)
        if recuperes:
            log.warning(
                "%d écoute(s) interrompue(s) par un arrêt brutal récupérée(s) dans %s",
                recuperes,
                settings.ondemand_dir,
            )

    # Le registre partagé entre la session du poste de terrain et celles des
    # opérateurs. Vivant sur `app.state` comme le classifieur, et pour la même
    # raison : il porte un état commun à toutes les sessions du processus.
    app.state.hub = ListenHub(settings.listen_queue_chunks)

    # Seul le rôle capture qualifie de l'audio. Le rôle admin n'instancie même
    # pas le classifieur : ni LiteRT, ni l'Interpreter non thread-safe, ni ses
    # ~95 Mo. C'est la moitié de l'intérêt du découpage — un blocage ou un
    # plantage d'inférence côté capture ne peut plus emporter le dashboard.
    classifier = None
    if settings.charge_classifieur:
        classifier = build_classifier(settings)
        classifier.load()
    app.state.classifier = classifier
    app.state.started_at = time.time()

    log.info(
        "rôle %s — backend %s, seuil %.2f, médias dans %s",
        settings.app_role,
        classifier.name if classifier else "aucun (rôle admin)",
        settings.dog_threshold,
        settings.media_dir,
    )
    try:
        yield
    finally:
        log.info("arrêt")
        if classifier is not None:
            classifier.close()
        await db.disconnect()


app = FastAPI(
    title="Noisygram",
    version=settings.server_version,
    description="Détection et historisation d'aboiements — YAMNet + LiteRT.",
    lifespan=lifespan,
)

# AUCUN CORSMiddleware (§5.6). Tout est same-origin : dashboard, page
# d'enregistrement, API et MP3 sont servis par ce même processus. Il n'existe
# aucun scénario cross-origin légitime dans le MVP, et une politique permissive
# sur un service qui accepte des uploads audio est un vrai risque pour zéro
# bénéfice.

# Le WebSocket du poste de terrain, ET le vrai /ws/listen : rôle capture.
if settings.sert_capture:
    app.include_router(ws_routes.router)

# L'API REST du dashboard : rôle admin uniquement.
if settings.sert_admin:
    app.include_router(events.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(qc_api.router)
    # Le RELAIS du direct : la page d'écoute vit sur l'admin, mais le poste est
    # connecté à capture. Cette route fait le pont.
    #
    # ⚠️ Elle est déclarée APRÈS `ws_routes`, et l'ordre compte pour
    # `APP_ROLE=all` : là, les deux routeurs déclarent `/ws/listen`, et c'est le
    # premier enregistré — le vrai — qui gagne. Inverser ces deux blocs ferait
    # que le serveur se relairait lui-même, en boucle.
    app.include_router(ws_proxy.router)

# Le panneau des samples d'écoute, sur les DEUX rôles — mais pas pour les
# mêmes raisons. Capture a le classifieur et analyse ; l'admin, lui, lit les
# mêmes fichiers (`./export` est monté des deux côtés) et relaie l'analyse.
if settings.ondemand_enabled and (settings.sert_capture or settings.sert_admin):
    app.include_router(ondemand_api.router, prefix="/api")

# La santé, sur les DEUX rôles : chacun a son propre healthcheck compose.
app.include_router(health.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def racine() -> RedirectResponse:
    # Chaque rôle renvoie vers SA page : un opérateur qui ouvre la racine du
    # port 4466 veut capturer, pas administrer.
    return RedirectResponse("/client/" if settings.app_role == "capture" else "/dashboard/")


# --- montages, EN DERNIER (G16) ---
#
# FastAPI apparie dans l'ordre d'enregistrement : un mount posé trop tôt avale
# silencieusement les routes déclarées après lui. Le symptôme est déroutant —
# les routes existent, elles sont dans /docs, et elles renvoient 404.

# Les MP3, sur les DEUX rôles : le dashboard les lit, et la page de capture
# joue le dernier extrait accepté.
app.mount("/media", ImmutableStaticFiles(directory=settings.media_dir), name="media")

# Chart.js, sur les deux rôles : monté explicitement plutôt que via le dossier
# static entier, parce que chaque rôle ne doit exposer que ses propres pages.
app.mount("/vendor", StaticFiles(directory=settings.static_dir / "vendor"), name="vendor")

if settings.sert_capture:
    # html=True fait servir index.html pour un dossier.
    app.mount(
        "/client",
        StaticFiles(directory=settings.static_dir / "client", html=True),
        name="client",
    )

if settings.sert_admin:
    app.mount(
        "/dashboard",
        StaticFiles(directory=settings.static_dir / "dashboard", html=True),
        name="dashboard",
    )
    # L'écoute directe, sur l'ADMIN et pas sur capture.
    #
    # Ce n'est pas un détail de rangement : `noisymic` ne doit rester joignable
    # que par le poste de terrain, alors que `noisy` peut être public. Une page
    # d'écoute exposée sur le premier serait exactement ce qu'il ne faut pas.
    #
    # Ce que la page fait ici alors que le poste est ailleurs : elle RELAIE.
    # `/ws/listen` (proxy.py), l'analyse d'un sample (`_relais_vers_capture`),
    # et les WAV — ces derniers étant lisibles des deux côtés puisque `./export`
    # est monté sur les deux services.
    app.mount(
        "/listen",
        StaticFiles(directory=settings.static_dir / "listen", html=True),
        name="listen",
    )
    if settings.ondemand_enabled:
        # Les WAV, pour la réécoute. Montés à part et non sous `/media` :
        # `/media` sert avec un cache immutable, ce qui figerait des fichiers
        # qu'on renomme et écrase à la main.
        app.mount(
            "/ondemand",
            SampleStaticFiles(directory=settings.ondemand_dir),
            name="ondemand",
        )
    # Les procédures. Montage conditionnel : `docs_dir` vient d'un volume monté
    # depuis l'hôte, et StaticFiles lève si le dossier n'existe pas. Une doc
    # absente ne doit pas empêcher le service de démarrer.
    if settings.docs_dir.is_dir():
        app.mount("/docs", MarkdownAsText(directory=settings.docs_dir), name="docs")
    else:
        log.warning(
            "dossier de documentation absent : %s — route /docs désactivée",
            settings.docs_dir,
        )
