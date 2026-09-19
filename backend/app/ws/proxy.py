"""Relais du direct : l'admin fait passer l'écoute vers capture.

POURQUOI CE MODULE EXISTE. L'écoute en direct ne peut pas vivre sur l'admin :
le poste de terrain y est connecté (le WebSocket `/ws/audio` est sur capture),
et c'est capture qui tient le hub qui diffuse. Mais l'interface d'écoute, elle,
doit être sur le nom d'hôte PUBLIC — `noisymic` ne doit rester joignable que par
le poste extérieur.

D'où ce pont : la page se connecte à `/ws/listen` sur l'admin, l'admin ouvre une
connexion vers `/ws/listen` sur capture, et fait passer les deux sens.

RIEN N'EST INTERPRÉTÉ ICI. On ne lit ni le protocole ni l'audio : on transporte
des trames. Toute la logique reste dans `session.py` et `listener.py`, côté
capture — un relais qui comprendrait le protocole finirait par en diverger, et
le symptôme serait un direct qui marche « presque ».

`websockets` est DÉJÀ une dépendance du projet (`tools/wstest.py` s'en sert pour
parler au serveur) : ce relais n'ajoute rien à l'image.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket
from websockets.asyncio.client import connect

from ..config import settings

log = logging.getLogger(__name__)

router = APIRouter()

WS_LISTEN_PATH = "/ws/listen"


def url_amont() -> str:
    """`http://capture:8000` → `ws://capture:8000/ws/listen`."""
    base = settings.capture_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + WS_LISTEN_PATH


async def _vers_amont(client: WebSocket, amont) -> None:
    """Ce que le navigateur envoie → capture."""
    while True:
        message = await client.receive()
        if message["type"] == "websocket.disconnect":
            return
        texte = message.get("text")
        donnees = message.get("bytes")
        if texte is not None:
            await amont.send(texte)
        elif donnees is not None:
            await amont.send(donnees)


async def _vers_client(client: WebSocket, amont) -> None:
    """Ce que capture diffuse → le navigateur.

    L'audio arrive en trames BINAIRES : les envoyer en texte les corromprait
    silencieusement, et le symptôme serait du bruit blanc au lieu du direct.
    """
    async for message in amont:
        if isinstance(message, (bytes, bytearray)):
            await client.send_bytes(bytes(message))
        else:
            await client.send_text(message)


async def relais(client: WebSocket, amont_url: str) -> None:
    """Ouvre le pont et le referme proprement, des deux côtés.

    Deux pompes concurrentes, et on s'arrête dès que L'UNE termine : si
    l'opérateur ferme son onglet, il n'y a plus personne à alimenter ; si
    capture tombe, il n'y a plus rien à écouter. Attendre les deux laisserait
    une connexion fantôme ouverte jusqu'au prochain hoquet réseau.
    """
    await client.accept()
    try:
        async with connect(amont_url, open_timeout=10, close_timeout=5) as amont:
            pompes = [
                asyncio.create_task(_vers_amont(client, amont)),
                asyncio.create_task(_vers_client(client, amont)),
            ]
            faites, en_cours = await asyncio.wait(
                pompes, return_when=asyncio.FIRST_COMPLETED
            )
            for t in en_cours:
                t.cancel()
            # On récupère les exceptions pour ne pas laisser de « exception was
            # never retrieved » dans le journal, qui masquerait la vraie.
            for t in pompes:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        log.warning("relais du direct impossible (%s) : %r", amont_url, exc)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


@router.websocket(WS_LISTEN_PATH)
async def listen_relais(websocket: WebSocket) -> None:
    """Le point d'entrée côté admin."""
    await relais(websocket, url_amont())
