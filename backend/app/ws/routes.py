"""Points d'entrée WebSocket — deux, et deux publics qui n'ont rien en commun.

`/ws/audio`  le POSTE DE TERRAIN : il envoie de l'audio, reçoit des ordres.
`/ws/listen` l'OPÉRATEUR : il reçoit de l'audio, envoie des ordres.

Les deux vivent ici parce qu'ils partagent le même `ListenHub` posé sur
`app.state` — c'est le hub qui les relie, et c'est le seul lien.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from . import protocol as P
from .hub import ListenHub
from .listener import ListenerSession
from .session import ConnectionSession

log = logging.getLogger(__name__)

router = APIRouter()

WS_PATH = "/ws/audio"
WS_LISTEN_PATH = "/ws/listen"


def _hub(websocket: WebSocket) -> ListenHub:
    """Le registre partagé, posé au démarrage comme le classifieur.

    Repli sur un hub neuf si `app.state` n'en porte pas — c'est le cas des
    outils de test qui montent l'application sans lifespan. Un hub orphelin
    rend l'écoute inerte (aucun poste enregistré), ce qui vaut mieux qu'une
    exception à la connexion.
    """
    hub = getattr(websocket.app.state, "hub", None)
    if hub is None:
        hub = ListenHub(settings.listen_queue_chunks)
        websocket.app.state.hub = hub
    return hub


@router.websocket(WS_PATH)
async def audio_socket(websocket: WebSocket) -> None:
    # Le classifieur vit sur app.state, posé au démarrage : on ne le reconstruit
    # jamais par connexion, il porte un Interpreter de 4 Mo et un verrou.
    classifier = websocket.app.state.classifier
    # Le nom du projet surveillé, posé au démarrage : le poste de terrain doit
    # savoir ce qu'il alimente. `getattr` parce que les outils de test montent
    # l'application sans passer par le lifespan.
    projet_nom = getattr(websocket.app.state, "projet_nom", None)
    session = ConnectionSession(
        websocket, settings, classifier, _hub(websocket), projet_nom=projet_nom
    )
    try:
        await session.run()
    except WebSocketDisconnect:
        # Départ propre du client : pas une anomalie, pas de trace.
        log.info("client déconnecté (session %s)", session.session_id)


@router.websocket(WS_LISTEN_PATH)
async def listen_socket(websocket: WebSocket) -> None:
    """Canal de l'opérateur. Aucun classifieur n'est nécessaire ici : c'est le
    poste de terrain qui classe, et le hub qui diffuse."""
    session = ListenerSession(websocket, settings, _hub(websocket))
    try:
        await session.run()
    except WebSocketDisconnect:
        log.info("opérateur déconnecté")
