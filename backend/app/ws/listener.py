"""Session d'un OPÉRATEUR qui écoute en direct (`/ws/listen`).

À ne pas confondre avec `session.py`, qui sert le POSTE DE TERRAIN. Les deux
parlent WebSocket, mais ils n'ont rien en commun :

  · le poste ENVOIE de l'audio et reçoit des ordres ; l'opérateur envoie des
    ordres et REÇOIT de l'audio ;
  · le poste fait un `hello` et tient un `protocol_version` ; l'opérateur non ;
  · un opérateur ne peut pas se faire passer pour un poste, et réciproquement,
    parce que les deux vocabulaires sont disjoints (`ws/protocol.py`).

Ce module est délibérément court : toute la mécanique de diffusion vit dans
`hub.py`, et toute la mécanique d'écoute (spool, classement, publication du WAV)
vit dans `session.py`. Il ne reste ici que l'aiguillage.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..config import Settings
from ..schemas import ListenBegin, ListenCancel
from . import protocol as P
from .hub import ListenHub, Listener

log = logging.getLogger(__name__)

# Les messages de `request_listen` qui SONT des codes du protocole. Tout le
# reste est une phrase destinée à l'opérateur, et lui donner un code inventé la
# ferait passer pour une erreur de protocole.
_CODES = frozenset({P.ERR_LISTENING, P.ERR_EPISODE_OPEN, P.ERR_NO_FIELD})


class ListenerSession:
    def __init__(self, ws: WebSocket, settings: Settings, hub: ListenHub) -> None:
        self.ws = ws
        self.settings = settings
        self.hub = hub
        self.listener: Listener | None = None
        self._writer: asyncio.Task | None = None
        # Le poste dont on écoute le micro. Retenu pour pouvoir annuler.
        self._field = None
        self._begun = False

    async def run(self) -> None:
        await self.ws.accept()
        log.info("opérateur connecté")
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is not None:
                    await self._on_text(text)
                # Aucune trame binaire n'est ACCEPTÉE d'un opérateur : il n'a
                # rien à envoyer. On l'ignore plutôt que d'en faire une erreur —
                # un navigateur peut toujours en produire une, et ce n'est pas
                # une raison de fermer la session.
        except WebSocketDisconnect:
            log.info("opérateur déconnecté")
        except Exception:  # noqa: BLE001
            log.exception("session opérateur interrompue")
        finally:
            await self._shutdown()

    async def _on_text(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await self._error("bad_json", "JSON invalide")
            return
        if not isinstance(payload, dict):
            await self._error("bad_json", "message JSON non-objet")
            return

        kind = payload.get("type")
        if kind == P.T_LISTEN_BEGIN:
            await self._begin(payload)
        elif kind == P.T_LISTEN_CANCEL:
            await self._cancel()
        else:
            await self._error(P.ERR_UNKNOWN_TYPE, f"type inconnu : {kind!r}")

    async def _begin(self, payload: dict) -> None:
        s = self.settings
        try:
            debut = ListenBegin.model_validate(payload)
        except ValidationError as exc:
            await self._error(P.ERR_BAD_JSON, f"listen_begin malformé : {exc.errors()}")
            return

        if not (s.ondemand_enabled and s.client_listen_enabled):
            await self._error(P.ERR_UNKNOWN_TYPE, "l'écoute à la demande est désactivée")
            return

        # ① Le poste à écouter, AVANT tout le reste : inutile d'attacher un
        #    auditeur si l'on ne saurait pas quel micro ouvrir.
        session, erreur = self.hub.resolve_field(debut.client_id)
        if session is None:
            code = P.ERR_FIELD_AMBIGUOUS if "postes connectés" in erreur else P.ERR_NO_FIELD
            await self._error(code, erreur)
            return

        # ② S'attacher, PUIS demander. L'ordre inverse perdrait les premiers
        #    morceaux : le poste peut se mettre à streamer avant que l'on soit
        #    inscrit sur la diffusion.
        auditeur = Listener(self.ws, s.listen_queue_chunks)
        self.listener = auditeur
        self._writer = asyncio.create_task(auditeur.drain())

        info = self.hub.attach(auditeur)
        if info is not None:
            # Une écoute tourne déjà : on s'y branche. `attach` et `offer` sont
            # tous deux synchrones, donc l'annonce est dans la file AVANT que la
            # boucle d'événements ne reprenne — aucun morceau ne peut la
            # précéder.
            auditeur.listen_id = info.listen_id
            auditeur.offer(
                P.listen_started(
                    listen_id=info.listen_id,
                    duration_ms=info.duration_ms,
                    chunk_ms=info.chunk_ms,
                    remaining_ms=info.remaining_ms(),
                    sample_rate=info.sample_rate,
                    joined=True,
                )
            )
            self._field = info.owner
            self._begun = True
            log.info("opérateur branché sur l'écoute %s en cours", info.listen_id)
            return

        self._field = session
        ok, message = await session.request_listen(debut.duration_ms)
        if not ok:
            # Course possible : une autre écoute a démarré pendant la demande.
            # On se branche dessus au lieu d'échouer — c'est exactement le cas
            # de deux onglets cliqués en même temps.
            info = self.hub.active
            if info is not None and message == P.ERR_LISTENING:
                auditeur.listen_id = info.listen_id
                auditeur.offer(
                    P.listen_started(
                        listen_id=info.listen_id,
                        duration_ms=info.duration_ms,
                        chunk_ms=info.chunk_ms,
                        remaining_ms=info.remaining_ms(),
                        sample_rate=info.sample_rate,
                        joined=True,
                    )
                )
                self._field = info.owner
                self._begun = True
                return
            self.hub.detach(auditeur)
            self.listener = None
            # `request_listen` rend soit un code du protocole (`listening`), soit
            # une phrase destinée à l'opérateur. On ne traduit que le premier :
            # inventer un code pour une phrase la ferait ressembler à une erreur
            # de protocole, ce qu'elle n'est pas.
            code = message if message and message in _CODES else P.ERR_INTERNAL
            await self._error(code, message or "écoute impossible")
            return

        # Le poste a accusé réception : `session` a déjà diffusé `listen_started`
        # sur la file de cet auditeur, avant le premier morceau.
        self._begun = True

    async def _cancel(self) -> None:
        """Arrête l'enregistrement en cours.

        On ne ferme PAS la session pour autant : l'opérateur doit encore
        recevoir le `listen_ended` avec le nom du fichier et son analyse. C'est
        sa page qui ferme la connexion quand elle n'a plus rien à attendre.
        """
        field = self._field
        if field is None or not self._begun:
            return
        await field.stop_listen()

    async def _error(self, code: str, message: str, fatal: bool = False) -> None:
        await self.ws.send_json(P.error(code, message, fatal=fatal))

    async def _shutdown(self) -> None:
        # On se DÉTACHE, mais l'écoute du poste CONTINUE : il a déjà payé le coût
        # de la mise en route, le WAV et l'épisode restent utiles même si
        # l'opérateur a fermé son onglet. Seul un `listen_cancel` explicite
        # interrompt — un lien qui hoquette ne doit pas détruire un
        # enregistrement.
        if self.listener is not None:
            self.hub.detach(self.listener)
            self.listener = None
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._writer = None
