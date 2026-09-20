"""Diffusion de l'écoute directe vers les opérateurs.

Un seul poste de terrain produit l'audio ; zéro, un ou plusieurs opérateurs
l'écoutent. Ce module est le seul endroit qui connaît les deux côtés, et sa
raison d'être tient en une phrase :

    **un auditeur lent ne doit jamais remonter la pression dans la boucle de
    réception du poste de terrain.**

C'est pour ça que `publish()` n'est pas une coroutine. Un `await` ici
insérerait le débit de l'opérateur dans le chemin critique du poste : sur un
lien qui hoquette, le poste serait ralenti — voire déconnecté — parce que
quelqu'un regarde. On préfère JETER des morceaux d'écoute, et le compter.

Ce que la diffusion n'utilise PAS, et c'est le point de correction central :
le `_send_lock` de la `ConnectionSession` du poste. `_send` le tient pendant
`await ws.send_json`, donc un socket d'opérateur lent bloquerait toutes les
réponses JSON dues au poste — y compris les accusés d'épisode. Chaque auditeur
a son propre socket et son propre écrivain.

Aucune dépendance à `session.py` : le hub ne connaît ses interlocuteurs que par
les quelques méthodes qu'il appelle. C'est ce qui le rend testable seul.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ListenInfo:
    """Ce qu'un auditeur a besoin de savoir sur l'écoute en cours."""

    listen_id: str
    duration_ms: int
    chunk_ms: int
    sample_rate: int
    owner: Any = None
    started_at: float = field(default_factory=time.monotonic)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_ms(self) -> int:
        return max(0, self.duration_ms - self.elapsed_ms())


class Listener:
    """Un opérateur qui écoute : UNE file, UNE tâche d'écriture.

    `deque(maxlen=…)` et non `asyncio.Queue`, pour trois raisons qui tiennent
    ensemble :

      · le drop du plus ancien est une ligne — `append` sur une deque pleine
        évince le plus ancien tout seul, sans code de gestion ;
      · `offer()` n'a pas besoin d'être une coroutine, donc le chemin critique
        du poste n'attend jamais ;
      · il n'y a pas de course entre un `get()` en vol et un `put_nowait()`,
        puisqu'il n'y a pas de `get()`.

    La file porte le JSON **et** le binaire, et une seule tâche draine les
    deux. Ce n'est pas une économie : c'est ce qui garantit que
    `listen_started` part AVANT le premier morceau de PCM. Deux files
    séparées, ou deux tâches, laisseraient l'ordre au hasard — et un
    `AudioBufferSourceNode` qui reçoit du son avant de connaître la fréquence
    d'échantillonnage ne sait pas le jouer.

    **Politique de perte : le plus ancien.** Jeter le plus récent laisserait la
    file pleine d'audio déjà périmé, donc une latence qui s'installe à demeure
    (4 × 200 ms de retard permanent) au lieu d'un trou. Pour de la surveillance
    directe, borner la latence prime sur la complétude — et les trous sont
    comptés puis affichés, jamais silencieux.
    """

    def __init__(self, ws: Any, queue_chunks: int) -> None:
        self.ws = ws
        self.buf: deque[bytes | dict] = deque(maxlen=max(1, queue_chunks))
        self.wake = asyncio.Event()
        self.dropped_chunks = 0
        self.closed = False
        self.listen_id: str | None = None

    def offer(self, item: bytes | dict) -> None:
        """Met en file sans jamais attendre. Appelé sur le chemin du poste."""
        if self.closed:
            return
        if len(self.buf) == self.buf.maxlen:
            # Compté AVANT l'append : la deque va évincer le plus ancien sans
            # le dire, et un trou non compté passerait pour du silence.
            self.dropped_chunks += 1
        self.buf.append(item)
        self.wake.set()

    async def drain(self) -> None:
        """La seule tâche qui écrit sur ce socket. Annulée par le `_shutdown`
        de la session opérateur."""
        while True:
            # `clear` AVANT de vider : l'ordre inverse perdrait un réveil
            # arrivé entre la fin du vidage et l'attente, et l'auditeur
            # resterait endormi avec de l'audio en file.
            self.wake.clear()
            while self.buf:
                item = self.buf.popleft()
                if isinstance(item, bytes):
                    await self.ws.send_bytes(item)
                else:
                    await self.ws.send_json(item)
            await self.wake.wait()

    def close(self) -> None:
        self.closed = True
        self.buf.clear()
        self.wake.set()


class ListenHub:
    """Registre des postes de terrain, des auditeurs, et de l'écoute en cours.

    Vit sur `app.state.hub` : une seule instance par processus, comme le
    classifieur, et pour la même raison — elle porte l'état partagé entre
    sessions.
    """

    def __init__(self, queue_chunks: int = 4) -> None:
        self._fields: set[Any] = set()
        self._listeners: set[Listener] = set()
        self._info: ListenInfo | None = None
        self.queue_chunks = queue_chunks

    # ------------------------------------------------------- postes de terrain

    def register_field(self, session: Any) -> None:
        self._fields.add(session)

    def unregister_field(self, session: Any) -> None:
        self._fields.discard(session)

    def postes(self) -> list[Any]:
        """Les postes de terrain HANDSHAKÉS, en copie.

        Copie et non vue : les sessions s'inscrivent et se désinscrivent
        pendant qu'on itère, et un `set` modifié en cours de boucle lève — au
        milieu d'une diffusion, donc après n'avoir prévenu que la moitié des
        postes. Le filtre est celui de `resolve_field` : un client qui n'a pas
        encore fait son `hello` n'est pas un poste.
        """
        return [s for s in set(self._fields) if getattr(s, "handshaked", False)]

    def resolve_field(self, client_id: str | None = None) -> tuple[Any | None, str | None]:
        """Le poste à écouter, ou la raison de ne pas savoir lequel.

        Rend `(session, None)` ou `(None, message)`. Un seul poste connecté est
        le cas normal ; zéro ou plusieurs sont des refus EXPLICITES. Choisir en
        silence serait le pire des comportements : l'opérateur écouterait un
        autre champ que le sien sans le savoir, et conclurait que la détection
        est cassée.
        """
        connus = [s for s in self._fields if getattr(s, "handshaked", False)]
        if client_id:
            connus = [s for s in connus if getattr(s, "client_id", None) == client_id]
        if not connus:
            detail = f"aucun poste « {client_id} » connecté" if client_id else "aucun poste connecté"
            return None, detail
        if len(connus) > 1:
            noms = sorted({getattr(s, "client_id", "?") or "?" for s in connus})
            return None, f"{len(connus)} postes connectés ({', '.join(noms)}) — précisez lequel"
        return connus[0], None

    # --------------------------------------------------------------- auditeurs

    @property
    def active(self) -> ListenInfo | None:
        return self._info

    def open_listen(self, info: ListenInfo) -> None:
        self._info = info

    def close_listen(self) -> None:
        self._info = None

    def attach(self, listener: Listener) -> ListenInfo | None:
        """Ajoute un auditeur. Rend l'écoute en cours s'il y en a une.

        Un second opérateur qui arrive pendant une écoute s'y BRANCHE au lieu
        de la refuser : c'est gratuit (la diffusion sait déjà le faire) et ça
        évite qu'un onglet oublié verrouille la fonctionnalité une minute.
        """
        self._listeners.add(listener)
        return self._info

    def detach(self, listener: Listener) -> None:
        self._listeners.discard(listener)
        listener.close()

    def publish(self, pcm: bytes) -> None:
        """Diffuse un morceau. JAMAIS de coroutine — voir l'en-tête du module.

        L'audio part AVANT l'écriture disque et AVANT le classement, dans
        `session.py`. C'est ce qui rend l'écoute directe indépendante du disque
        et du modèle : une réanalyse de 320 ms lancée depuis le panneau
        décalera des scores, jamais le son.
        """
        for auditeur in self._listeners:
            auditeur.offer(pcm)

    def broadcast(self, message: dict) -> None:
        """Diffuse un message de contrôle, dans la même file que l'audio."""
        for auditeur in self._listeners:
            auditeur.offer(message)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    @property
    def dropped_chunks(self) -> int:
        """Morceaux jetés, tous auditeurs confondus.

        Remonté dans `listen_progress` : sans ce chiffre, un trou dans l'audio
        direct se lit comme du silence dans la pièce — c'est-à-dire la
        conclusion exactement inverse de la vérité.
        """
        return sum(a.dropped_chunks for a in self._listeners)

    def shutdown(self) -> None:
        for auditeur in list(self._listeners):
            auditeur.close()
        self._listeners.clear()
        self._info = None
