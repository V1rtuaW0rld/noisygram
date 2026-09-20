"""Une session WebSocket : un client, N segments en vol au maximum.

Le flux d'un segment accepté, dans cet ordre et pas un autre :

    1. qualifier            → si refusé, on s'arrête là (§5.2)
    2. réserver l'id        → le nom du MP3 en dépend
    3. encoder + écrire     → atomiquement
    4. insérer en base      → le chemin et la taille sont déjà connus

L'étape 2 existe parce que le fichier est nommé d'après l'identifiant de
l'événement, ce qui permet de passer de la ligne au fichier sans ambiguïté.
Elle coûte un aller-retour, uniquement sur les segments ACCEPTÉS — donc sur
une minorité des déclenchements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import WebSocket
from pydantic import ValidationError

from .. import db, debugdump, qc_client
from ..audio.mp3 import encode_mp3
from ..audio.pcm import pcm16_to_float32
from ..audio.resample import TARGET_SR, resample_to_16k
from ..audio.wav import wav_bytes
from ..config import Settings
from ..schemas import ClientHello, ClientPing, ListenEnd, ListenStart, SegmentStart, StreamStart
from ..storage import media, ondemand
from . import protocol as P
from .hub import ListenHub, ListenInfo
from .stream import Bilan, EpisodeWriter, StreamState, WindowScorer, compter_evenements

log = logging.getLogger(__name__)

# Fenêtres en attente de classement. Au-delà, on compte un `dropped` au lieu de
# bloquer la réception : un épisode dont on n'a pas classé toutes les fenêtres
# reste exploitable, à condition de l'écrire dans la ligne.
FILE_FENETRES = 8

# Contexte gardé de part et d'autre de la première et de la dernière fenêtre
# retenue. Assez pour entendre l'événement arriver et repartir, pas assez pour
# garder du vide.
ROGNAGE_TETE_MS = 1500
ROGNAGE_QUEUE_MS = 1000

# Durée pendant laquelle une trame binaire arrivée après la clôture d'un épisode
# est absorbée en silence. Assez large pour couvrir un aller-retour réseau, assez
# courte pour qu'une vraie désynchronisation du protocole reste signalée.
FENETRE_TARDIVE_S = 5.0

# Délai laissé au poste pour accuser réception d'un `listen_request`. Au-delà,
# on rend la main à l'opérateur avec un message nommant les causes possibles
# plutôt que de le laisser devant une page muette.
LISTEN_ACK_TIMEOUT_S = 5.0

# Insertion d'un épisode. Même clause WHERE obligatoire que pour les segments :
# l'unicité (client_id, client_seq) est portée par un index PARTIEL.
EPISODE_INSERT_SQL = """
INSERT INTO events (
    id, detected_at, received_at, client_captured_at, client_id, client_seq,
    noisy_score, bark_score, mean_noisy_score, duration_ms, sample_rate,
    mp3_path, mp3_bytes, backend, model_version, top_classes,
    noisy_count, partial, stopped_reason, window_count, wav_name, qc_score, qc_valid,
    projet_id
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,
          nullif(current_setting('app.projet_id', true), '')::int)
ON CONFLICT (client_id, client_seq) WHERE client_seq IS NOT NULL DO NOTHING
RETURNING id
"""

# ⚠️ La clause WHERE est OBLIGATOIRE : l'unicité (client_id, client_seq) est
# portée par un index PARTIEL (WHERE client_seq IS NOT NULL). Sans la
# répéter ici, PostgreSQL ne trouve aucun index correspondant et rejette la
# requête — « there is no unique or exclusion constraint matching the
# ON CONFLICT specification ». Le document de conception §7 l'omettait.
INSERT_SQL = """
INSERT INTO events (
    id, detected_at, received_at, client_captured_at, client_id, client_seq,
    noisy_score, bark_score, mean_noisy_score, duration_ms, sample_rate,
    mp3_path, mp3_bytes, backend, model_version, top_classes,
    projet_id
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
          nullif(current_setting('app.projet_id', true), '')::int)
ON CONFLICT (client_id, client_seq) WHERE client_seq IS NOT NULL DO NOTHING
RETURNING id
"""

SELECT_EXISTING_SQL = """
SELECT id, mp3_path, mp3_bytes FROM events
WHERE client_id = $1 AND client_seq = $2
"""


def _rogner(st, bornes: tuple[int, int]) -> tuple[int, int]:
    """L'intervalle à garder, contexte compris, en échantillons.

    Rogner la tête et la queue, AVANT d'encoder : les trente secondes de silence
    terminal qui ont déclenché la fin ne sont pas de la preuve, c'est du vide —
    mais on garde un peu de contexte de chaque côté, parce qu'un fichier qui
    commence au milieu d'un « wouf » n'est pas écoutable.

    Fonction PURE et extraite parce que les deux chemins de finalisation
    (épisode et écoute) doivent rogner IDENTIQUEMENT : c'est la seule
    arithmétique qui serait dupliquée, et un demi-hop d'écart entre les deux
    serait invisible tout en décalant tous les fichiers.
    """
    plomb = int(ROGNAGE_TETE_MS * st.sample_rate / 1000)
    queue = int(ROGNAGE_QUEUE_MS * st.sample_rate / 1000)
    premier = max(0, bornes[0] - plomb)
    dernier = min(st.samples, bornes[1] + queue)
    return premier, dernier


def _captured_at_utc(ms: int | None) -> datetime | None:
    """Horloge CLIENT, à titre de diagnostic seulement (§5.5).

    Le vieux PC Windows en extérieur est exactement le genre de machine dont
    l'horloge dérive de plusieurs heures en silence. Elle n'entre jamais dans
    `detected_at`. On la stocke quand même : après un mois, comparer les deux
    colonnes dira si l'horloge a dérivé, ce qui serait autrement invisible.
    """
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class ConnectionSession:
    def __init__(
        self,
        ws: WebSocket,
        settings: Settings,
        classifier,
        hub: ListenHub | None = None,
        projet_nom: str | None = None,
    ) -> None:
        self.ws = ws
        self.settings = settings
        self.classifier = classifier
        # Ce que cette capture surveille, annoncé au poste dans le hello_ack.
        self.projet_nom = projet_nom
        # Le registre partagé entre sessions. Peut être None (outils de test) :
        # tout le chemin d'écoute est alors simplement inerte, ce qui vaut mieux
        # que d'obliger chaque appelant à en construire un.
        self.hub = hub

        self.session_id = uuid.uuid4().hex
        self.client_id: str | None = None
        self.client_version: str | None = None
        self.handshaked = False

        # Segment dont on attend la trame binaire.
        self._pending_segment: SegmentStart | None = None
        # Métadonnées refusées : la trame binaire qui suit doit être avalée
        # sans bruit, sinon on répondrait deux erreurs pour un seul segment.
        self._discard_next = False

        # Épisode en cours. UN SEUL à la fois : c'est la vraie borne de charge,
        # le garde-tempête du client ne bornant que les DÉCLENCHEMENTS — un
        # épisode de trois minutes n'en produit aucun.
        self._stream = None
        # Un `stream_start` refusé doit faire avaler TOUTES les trames du flux,
        # pas une seule : `_discard_next` est unaire, et le laisser tel quel
        # produirait une erreur par morceau, soixante fois de suite.
        self._discard_stream = False
        self._dernier_flux_a = 0.0
        # Instant de clôture du dernier épisode, pour absorber les morceaux
        # arrivés en retard (voir `_on_binary`).
        self._flux_clos_a = 0.0

        # Écoute à la demande en cours. SECOND emplacement d'audio, distinct de
        # `_stream` : c'est ce qui rend l'exclusivité structurelle plutôt que
        # dépendante d'un drapeau qu'on pourrait oublier de tester.
        self._listen = None
        # Écoute refusée à l'ouverture : on avale ses morceaux jusqu'à
        # `listen_end`, sinon on produirait une erreur par morceau.
        self._discard_listen = False
        self._listen_clos_a = 0.0
        # L'opérateur attend l'acquittement du poste. Résolu par
        # `_on_listen_start`, ou par l'échec/timeout.
        self._listen_ack: asyncio.Future | None = None
        # Ce que l'opérateur a demandé, le temps que le poste réponde.
        self._listen_demande: tuple[str, int, int] | None = None
        # L'écoute en cours, telle que l'opérateur la voit.
        self._listen_info: ListenInfo | None = None

        self._pending = 0
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._fatal = False
        self._stats = {"accepted": 0, "rejected": 0, "errors": 0, "busy": 0, "streams": 0}

    # ------------------------------------------------------------- boucle

    async def run(self) -> None:
        await self.ws.accept()
        log.info("session %s ouverte", self.session_id)
        if self.hub is not None:
            self.hub.register_field(self)
        try:
            while True:
                # receive() brut, et non receive_text()/receive_bytes() : on a
                # besoin de distinguer les deux, et les helpers lèvent une
                # exception sur le type inattendu.
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect" or self._fatal:
                    break
                text = message.get("text")
                data = message.get("bytes")
                if text is not None:
                    await self._on_text(text)
                elif data is not None:
                    await self._on_binary(data)
        except Exception:  # noqa: BLE001
            log.exception("session %s interrompue", self.session_id)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        # Un épisode est un ÉTAT, pas une tâche : l'annulation ci-dessous ne le
        # verrait pas, et il disparaîtrait sans trace. Avant, une coupure coûtait
        # un clip de 3 s ; maintenant elle coûterait trois minutes d'audio — le
        # seul cas où l'on peut perdre une preuve. On finalise donc D'ABORD, sous
        # un délai borné pour qu'un disque lent ne bloque pas la fermeture.
        #
        # L'écoute passe en PREMIER, et pour une raison plus forte encore : son
        # WAV est la seule copie d'une prise de son que quelqu'un a demandée
        # explicitement. Un épisode peut être reconstitué par le prochain
        # déclenchement ; l'écoute, non.
        if self._listen is not None:
            try:
                await asyncio.wait_for(
                    self._finalize_listen(self._listen, "connection_lost"), timeout=10
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.warning("écoute non finalisée à la fermeture : %s", exc)

        # L'opérateur qui attend encore un acquittement ne doit pas rester
        # suspendu jusqu'à son timeout : le poste est parti.
        self._resolve_listen_ack(False, "le poste de terrain s'est déconnecté")

        if self._stream is not None:
            try:
                await asyncio.wait_for(
                    self._finalize_stream(self._stream, "connection_lost"), timeout=10
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.warning("épisode non finalisé à la fermeture : %s", exc)

        if self.hub is not None:
            self.hub.unregister_field(self)

        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info(
            "session %s fermée (client=%s) — %s",
            self.session_id,
            self.client_id,
            self._stats,
        )

    # ------------------------------------------------------------ entrant

    async def _on_text(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            await self._fail(P.ERR_BAD_JSON, f"JSON invalide : {exc}")
            return
        if not isinstance(payload, dict):
            await self._fail(P.ERR_BAD_JSON, "message JSON non-objet")
            return

        kind = payload.get("type")

        if kind == P.T_HELLO:
            await self._on_hello(payload)
            return

        if not self.handshaked:
            # Le premier message DOIT être `hello`. On le traite comme fatal,
            # alors que le document ne réserve ce drapeau qu'au mismatch de
            # version : c'est la même classe de problème — un client qui ne
            # sait pas parler le protocole ne se répare pas en se reconnectant,
            # et le laisser reboucler indéfiniment ne sert personne.
            await self._fail(
                P.ERR_UNKNOWN_TYPE,
                "premier message : 'hello' attendu",
                fatal=True,
            )
            return

        if kind == P.T_SEGMENT_START:
            await self._on_segment_start(payload)
        elif kind == P.T_STREAM_START:
            await self._on_stream_start(payload)
        elif kind == P.T_STREAM_END:
            await self._on_stream_end(payload)
        elif kind == P.T_LISTEN_START:
            await self._on_listen_start(payload)
        elif kind == P.T_LISTEN_END:
            await self._on_listen_end(payload)
        elif kind == P.T_PING:
            await self._send(P.pong(payload.get("t")))
        else:
            await self._fail(P.ERR_UNKNOWN_TYPE, f"type inconnu : {kind!r}")

    async def _on_hello(self, payload: dict) -> None:
        try:
            hello = ClientHello.model_validate(payload)
        except ValidationError as exc:
            await self._fail(P.ERR_BAD_JSON, f"hello malformé : {exc.errors()}")
            return

        if hello.protocol_version != P.PROTOCOL_VERSION:
            await self._fail(
                P.ERR_UNKNOWN_TYPE,
                f"protocol_version {hello.protocol_version}, le serveur parle "
                f"la {P.PROTOCOL_VERSION}",
                fatal=True,
            )
            return

        self.client_id = hello.client_id
        self.client_version = hello.app_version
        self.handshaked = True
        log.info(
            "hello de %s (app %s, %s Hz, ua=%s)",
            hello.client_id,
            hello.app_version,
            hello.device_sample_rate,
            (hello.user_agent or "")[:60],
        )

        s = self.settings
        await self._send(
            P.hello_ack(
                session_id=self.session_id,
                server_version=s.server_version,
                client_id=hello.client_id,
                projet_nom=self.projet_nom,
                classifier_info=self.classifier.describe(),
                limits={
                    "max_segment_bytes": s.max_segment_bytes,
                    "min_sample_rate": s.min_sample_rate,
                    "max_sample_rate": s.max_sample_rate,
                    "max_segment_ms": s.max_segment_ms,
                    "max_pending": s.max_pending,
                },
                config_patch={
                    "cooldown_ms": s.client_cooldown_ms,
                    "trigger_ratio": s.client_trigger_ratio,
                    "min_rms_floor": s.client_min_rms_floor,
                    "storm_max": s.client_storm_max,
                    "storm_window_ms": s.client_storm_window_ms,
                    "storm_suspend_ms": s.client_storm_suspend_ms,
                    # La longueur d'un épisode se règle d'ici : c'est elle qui
                    # décide de la place prise sur le disque.
                    "stream_enabled": s.client_stream_enabled,
                    "stream_silence_ms": s.client_stream_silence_ms,
                    "stream_max_ms": s.client_stream_max_ms,
                    # Le droit d'être écouté à la demande. Poussé au poste et
                    # pas seulement gardé ici : couper la fonctionnalité côté
                    # serveur sans que le poste le sache laisserait la page
                    # d'écoute attendre un audio qui ne viendrait jamais.
                    "listen_enabled": s.client_listen_enabled and s.ondemand_enabled,
                },
            )
        )

    async def _on_segment_start(self, payload: dict) -> None:
        try:
            seg = SegmentStart.model_validate(payload)
        except ValidationError as exc:
            await self._fail(P.ERR_BAD_JSON, f"segment_start malformé : {exc.errors()}")
            self._discard_next = True
            return

        s = self.settings

        # Les métadonnées sont validées AVANT que la trame binaire n'arrive :
        # on peut refuser sans attendre 288 Ko inutiles, et sans avoir à les
        # analyser. La trame qui suit est avalée par _discard_next.
        if seg.format != "s16le":
            await self._reject_segment(
                P.ERR_BAD_FORMAT, f"format {seg.format!r}, attendu 's16le'", seg.seq
            )
            return
        if seg.channels != 1:
            await self._reject_segment(
                P.ERR_BAD_FORMAT,
                f"{seg.channels} canaux, attendu 1 — le downmix (L+R)/2 est fait "
                "côté client",
                seg.seq,
            )
            return
        if not (s.min_sample_rate <= seg.sample_rate <= s.max_sample_rate):
            await self._reject_segment(
                P.ERR_BAD_SAMPLE_RATE,
                f"{seg.sample_rate} Hz hors de "
                f"[{s.min_sample_rate}, {s.max_sample_rate}]",
                seg.seq,
            )
            return
        if seg.num_samples <= 0:
            await self._reject_segment(
                P.ERR_BAD_LENGTH, f"num_samples = {seg.num_samples}", seg.seq
            )
            return
        duration_ms = round(seg.num_samples / seg.sample_rate * 1000)
        if duration_ms > s.max_segment_ms:
            await self._reject_segment(
                P.ERR_PAYLOAD_TOO_LARGE,
                f"segment de {duration_ms} ms, maximum {s.max_segment_ms} ms",
                seg.seq,
            )
            return

        self._pending_segment = seg

    async def _on_binary(self, data: bytes) -> None:
        # ⚠️ L'ORDRE DE CES DEUX PREMIERS TESTS EST PORTEUR, et c'est le seul
        #    endroit du fichier où il l'est vraiment.
        #
        #    Une écoute refusée (parce qu'un épisode est ouvert) a posé
        #    `_discard_listen`, mais son `_stream` à lui est NON NUL. Tester
        #    `_stream` en premier verserait donc les morceaux de l'écoute
        #    refusée DANS l'épisode en cours. Les deux flux sont à 16 kHz mono :
        #    le résultat serait un épisode pollué par du son étranger, à la
        #    bonne fréquence, donc SANS AUCUN SIGNAL D'ERREUR. Un
        #    enregistrement faux qui a l'air juste — exactement ce que ce
        #    projet passe son temps à refuser.
        #
        # ① Une écoute ouverte absorbe TOUTE trame binaire.
        if self._listen is not None:
            await self._on_listen_chunk(data)
            return
        # ② Écoute refusée à l'ouverture : on avale jusqu'à `listen_end`.
        if self._discard_listen:
            return
        # ③ Un épisode ouvert absorbe TOUTE trame binaire. C'est cette ligne
        #    qui fait coexister les deux chemins : un client ancien n'ouvre
        #    jamais de flux, un client neuf n'envoie jamais de segment.
        if self._stream is not None:
            await self._on_stream_chunk(data)
            return
        # ④ Un flux refusé à l'ouverture : on avale ses morceaux jusqu'à
        #    `stream_end`. `_discard_next` est UNAIRE et n'avalerait que le
        #    premier, produisant une erreur par morceau — soixante erreurs pour
        #    un seul refus.
        if self._discard_stream:
            return

        if self._discard_next:
            self._discard_next = False
            return

        seg = self._pending_segment
        if seg is None:
            # Un morceau EN RETARD sur un épisode qui vient de se clore n'est
            # pas une erreur : c'est du son parfaitement valide, parti avant que
            # `stream_stop` n'atteigne le client. Le serveur peut clore de son
            # propre chef — silence mesuré chez lui, expiration de la durée
            # maximale — et la trame est alors déjà en vol.
            #
            # Sans cette fenêtre, CHAQUE épisode terminé côté serveur affichait
            # une erreur rouge chez le client, pour un épisode accepté et
            # enregistré. Un voyant rouge qui ne signale rien apprend à ignorer
            # les voyants rouges.
            if time.monotonic() - self._flux_clos_a < FENETRE_TARDIVE_S:
                return
            await self._fail(
                P.ERR_BAD_LENGTH, "trame binaire sans segment_start préalable"
            )
            return
        self._pending_segment = None

        s = self.settings
        if len(data) > s.max_segment_bytes:
            await self._fail(
                P.ERR_PAYLOAD_TOO_LARGE,
                f"segment de {len(data)} octets, limite {s.max_segment_bytes}",
                seg.seq,
            )
            return

        expected = seg.num_samples * seg.channels * 2
        if len(data) != expected:
            await self._fail(
                P.ERR_BAD_LENGTH,
                f"trame de {len(data)} octets, num_samples × channels × 2 = "
                f"{expected}",
                seg.seq,
            )
            return

        # Backpressure : au-delà de max_pending segments en vol, on refuse au
        # lieu de mettre en file. Une file silencieuse finirait en croissance
        # mémoire non bornée puis en OOM kill (§6).
        if self._pending >= s.max_pending:
            self._stats["busy"] += 1
            await self._fail(
                P.ERR_BUSY,
                f"{self._pending} segments en vol, limite {s.max_pending}",
                seg.seq,
            )
            return

        self._pending += 1
        task = asyncio.create_task(self._process(seg, data))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._pending -= 1
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # _process attrape déjà tout : arriver ici signale un bug à nous,
            # pas un incident réseau.
            log.error("tâche de segment en échec", exc_info=task.exception())

    # ------------------------------------------------------- traitement

    async def _process(self, seg: SegmentStart, payload: bytes) -> None:
        s = self.settings

        try:
            x = pcm16_to_float32(payload, channels=seg.channels)
            x16 = resample_to_16k(x, seg.sample_rate)
        except Exception as exc:  # noqa: BLE001
            await self._fail(P.ERR_DECODE_ERROR, f"{type(exc).__name__}: {exc}", seg.seq)
            return

        try:
            result = await self.classifier.classify_async(x16)
        except Exception as exc:  # noqa: BLE001
            log.exception("classification du segment %s", seg.seq)
            await self._fail(
                P.ERR_CLASSIFY_ERROR, f"{type(exc).__name__}: {exc}", seg.seq
            )
            return

        # Ce que YAMNet a RÉELLEMENT entendu. Les scores disent qu'il refuse ;
        # ils ne disent pas si c'est l'audio qui est mauvais ou le modèle qui
        # n'y reconnaît rien — un WAV qu'on peut écouter répond en trois
        # secondes, là où un journal ne répond pas du tout.
        debugdump.dump(
            x16,
            f"seg{seg.seq}_score{result.noisy_score:.3f}_{seg.sample_rate}Hz",
            self.settings.debug_dump_dir,
            self.settings.debug_dump,
            self.settings.debug_dump_keep,
        )

        # detected_at est calculé CÔTÉ SERVEUR (§5.5) : l'horloge du vieux PC
        # Windows n'entre jamais dans l'axe de temps du dashboard.
        post_roll_ms = max(0, min(seg.post_roll_ms or 0, s.max_segment_ms))
        received_at = datetime.now(timezone.utc)
        detected_at = received_at - timedelta(milliseconds=post_roll_ms)
        duration_ms = round(seg.num_samples / seg.sample_rate * 1000)

        accepted = result.noisy_score >= s.noisy_threshold

        if not accepted:
            if s.save_rejected:
                await self._save_rejected(seg, x16, detected_at)
            self._stats["rejected"] += 1
            # On n'encode RIEN : c'est tout l'intérêt de décider avant (§5.2).
            # Le comportement observable est le même qu'« écrire puis
            # supprimer », sans l'encodage, l'écriture et le unlink sur chaque
            # faux positif — soit la majorité des déclenchements par jour.
            await self._send(
                P.segment_result(
                    seq=seg.seq,
                    event_id=None,
                    accepted=False,
                    result=result,
                    threshold=s.noisy_threshold,
                    duration_ms=duration_ms,
                    mp3_url=None,
                    mp3_bytes=None,
                    reason=P.REASON_BELOW,
                )
            )
            return

        try:
            event_id, mp3_rel, mp3_bytes, duplicate = await self._store(
                seg, result, x16, detected_at, received_at, duration_ms
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("stockage du segment %s", seg.seq)
            await self._fail(P.ERR_INTERNAL, f"stockage : {exc!r}", seg.seq)
            return

        self._stats["accepted"] += 1
        await self._send(
            P.segment_result(
                seq=seg.seq,
                event_id=event_id,
                accepted=True,
                result=result,
                threshold=s.noisy_threshold,
                duration_ms=duration_ms,
                mp3_url=media.url_for(mp3_rel),
                mp3_bytes=mp3_bytes,
                reason=P.REASON_DUPLICATE if duplicate else P.REASON_OK,
            )
        )

    # ------------------------------------------------------------ épisodes

    async def _on_stream_start(self, payload: dict) -> None:
        s = self.settings
        seq = payload.get("seq")

        # Un `stream_start` vu ici signifie que tous les morceaux d'écoute qui
        # pouvaient le précéder ont déjà été consommés ou jetés : WebSocket
        # garantit l'ordre sur un même socket, et aucun morceau d'une écoute
        # close ne peut suivre sans un nouveau `listen_request`. C'est donc le
        # point naturel pour baisser ce drapeau — sans quoi une écoute refusée
        # ferait avaler le prochain épisode, morceau par morceau, en silence.
        self._discard_listen = False

        if self._listen is not None:
            # Surtout PAS `busy` : le client traite `busy` en remettant le
            # segment en file, or `inflight[seq]` d'un flux ne porte pas de PCM
            # — `flushQueue` appellerait `sendSegment()` sur des métadonnées et
            # lèverait un TypeError au milieu du gestionnaire.
            await self._reject_stream(
                P.ERR_LISTENING, "une écoute est en cours sur cette session", seq
            )
            return

        if self._stream is not None:
            await self._reject_stream(P.ERR_STREAM_OPEN, "un épisode est déjà ouvert", seq)
            return

        try:
            entete = StreamStart.model_validate(payload)
        except ValidationError as exc:
            await self._reject_stream(P.ERR_BAD_JSON, str(exc), seq)
            return
        seq = entete.seq

        if entete.format != "s16le":
            await self._reject_stream(P.ERR_BAD_FORMAT, f"format {entete.format!r}", seq)
            return
        if entete.channels != 1:
            await self._reject_stream(P.ERR_BAD_FORMAT, f"{entete.channels} canaux", seq)
            return

        # ⚠️ 16 kHz EXIGÉ — la validation la plus importante de ce chemin.
        # `resample_to_16k` court-circuite quand la source est déjà à 16 kHz.
        # Appelé PAR MORCEAU, soxr introduirait une discontinuité de filtre à
        # chaque frontière : un clic par seconde, et des scores qui restent
        # globalement bons. Un refus franc vaut mieux qu'une dégradation que
        # personne ne verrait.
        if entete.sample_rate != TARGET_SR:
            await self._reject_stream(
                P.ERR_BAD_SAMPLE_RATE,
                f"un épisode exige {TARGET_SR} Hz, reçu {entete.sample_rate}",
                seq,
            )
            return

        maintenant = time.monotonic()
        if (maintenant - self._dernier_flux_a) * 1000 < s.stream_min_interval_ms:
            await self._reject_stream(P.ERR_BUSY, "épisode trop rapproché du précédent", seq)
            return

        # Le disque est vérifié AVANT d'écrire un octet : le temporaire s'écrit
        # pour TOUS les déclenchements, gardés ou non.
        libre = media.free_bytes(s.media_dir)
        if libre is not None and libre < s.min_free_bytes:
            await self._reject_stream(P.ERR_LOW_DISK, f"{libre} octets libres", seq)
            return

        # Rejeu : le client se reconnecte et renvoie un épisode déjà stocké.
        # On le détecte MAINTENANT et non à l'insertion, sinon on classifierait
        # et encoderait trois minutes pour tout jeter ensuite.
        if self.client_id is not None:
            existant = await db.fetchrow(SELECT_EXISTING_SQL, self.client_id, seq)
            if existant is not None:
                self._discard_stream = True
                await self._send(
                    P.stream_ack(
                        seq=seq,
                        stream_id=self.session_id,
                        duplicate=True,
                        max_stream_ms=0,
                        max_chunk_bytes=s.stream_chunk_max_bytes,
                        silence_ms=s.stream_silence_ms,
                    )
                )
                # Et la réponse TERMINALE, tout de suite. La règle est « un ack
                # accepté ⇒ exactement un segment_result ou un error, jamais
                # zéro » : sans elle, le client attendrait indéfiniment une
                # réponse qui ne viendrait qu'à la fin d'un flux qu'on jette, et
                # garderait son `inflight[seq]` pour toujours.
                await self._send(
                    P.segment_result(
                        seq=seq,
                        event_id=existant["id"],
                        accepted=True,
                        result=Bilan(0.0, None, 0.0),
                        threshold=s.noisy_threshold,
                        duration_ms=0,
                        mp3_url=media.url_for(existant["mp3_path"]),
                        mp3_bytes=existant["mp3_bytes"],
                        reason=P.REASON_DUPLICATE,
                    )
                )
                return

        recu = datetime.now(timezone.utc)
        st = StreamState(
            seq=seq,
            sample_rate=entete.sample_rate,
            pre_roll_samples=max(0, entete.pre_roll_samples),
            received_at=recu,
            writer=EpisodeWriter(
                s.media_dir, media.spool_relpath(self.session_id, seq, s.app_tz, recu)
            ),
            scorer=WindowScorer(),
            queue=asyncio.Queue(maxsize=FILE_FENETRES),
        )
        st.derniere_trame = maintenant
        st.dernier_bruyant = maintenant
        st.dernier_progres = maintenant
        self._stream = st
        self._flux_clos_a = 0.0
        self._dernier_flux_a = maintenant
        self._stats["streams"] += 1

        st.task = asyncio.create_task(self._drain_windows(st))
        st.watchdog = asyncio.create_task(self._stream_watchdog(st))
        self._tasks.add(st.task)
        self._tasks.add(st.watchdog)

        await self._send(
            P.stream_ack(
                seq=seq,
                stream_id=self.session_id,
                duplicate=False,
                max_stream_ms=s.max_stream_ms,
                max_chunk_bytes=s.stream_chunk_max_bytes,
                silence_ms=s.stream_silence_ms,
            )
        )

    async def _reject_stream(self, code: str, message: str, seq) -> None:
        """Refuse l'ouverture ET avale tout ce qui suit jusqu'à `stream_end`.

        Sans le drapeau, les soixante morceaux qui arrivent ensuite produiraient
        chacun leur erreur — `_discard_next` est unaire et n'avalerait que le
        premier.
        """
        self._discard_stream = True
        await self._fail(code, message, seq if isinstance(seq, int) else None)

    async def _on_stream_chunk(self, data: bytes) -> None:
        st = self._stream
        s = self.settings
        st.derniere_trame = time.monotonic()
        st.chunks += 1
        st.bytes_recus += len(data)

        # Le chemin segment compare `len(data)` à `num_samples × 2` en ÉGALITÉ
        # STRICTE. Le copier ici ferait échouer TOUS les épisodes au premier
        # morceau : le pré-roll est plus long que les suivants.
        if len(data) % 2 or len(data) > s.stream_chunk_max_bytes:
            await self._abort_stream(
                P.ERR_BAD_LENGTH,
                f"morceau de {len(data)} octets (limite {s.stream_chunk_max_bytes}, paire exigée)",
            )
            return

        # ⚠️ BORNE DE VOLUME : ON GARDE CE QU'ON A, ON NE JETTE PAS.
        #
        # Elle était traitée comme une borne de sécurité, donc `_abort_stream`,
        # donc le spool détruit. C'était faux : dépasser un volume ne dit RIEN
        # de la qualité de l'audio. Une source qui dure deux minutes produit un
        # épisode long, parfaitement légitime — et il était jeté. Mesuré sur le
        # terrain : 132 s d'audio perdus d'un coup.
        #
        # Seul ce qui est MALFORMÉ mérite d'être jeté (`_abort_stream` plus
        # haut, sur une longueur impaire ou un morceau démesuré) : là, oui, on
        # ne sait plus ce qu'on lit. Un volume, non.
        if st.bytes_recus > s.max_stream_bytes or st.chunks > s.max_stream_chunks:
            log.warning(
                "épisode %s : borne de volume atteinte (%d octets, %d morceaux) "
                "— clos et CONSERVÉ",
                st.seq,
                st.bytes_recus,
                st.chunks,
            )
            await self._send(P.stream_stop(seq=st.seq, reason=P.REASON_MAX_DURATION))
            await self._finalize_stream(st, P.REASON_MAX_DURATION)
            return

        st.writer.write(data)
        st.samples += len(data) // 2
        x = pcm16_to_float32(data, channels=1)

        # Le serveur mesure son PROPRE niveau : il ne dépend pas du client pour
        # décider que l'épisode est terminé. Sans ça, un client bogué streame
        # indéfiniment et seul `max_stream_ms` l'arrête.
        if x.size:
            rms = float(np.sqrt(np.mean(np.square(x))))
            st.rms_vus.append(rms)
            if len(st.rms_vus) >= 4:
                fond = float(np.median(st.rms_vus))
                seuil_bruit = max(fond * s.client_trigger_ratio, s.client_min_rms_floor)
                if rms > seuil_bruit:
                    st.dernier_bruyant = st.derniere_trame

        for offset, fenetre in st.scorer.feed(x):
            # On ATTEND une place, avec un délai borné — on ne jette pas.
            #
            # Jeter une fenêtre ne coûte pas qu'un score : le fichier est rogné
            # sur les fenêtres retenues, donc une fenêtre perdue TRONQUE
            # l'enregistrement. Un client qui envoie son épisode en rafale après
            # une coupure réseau verrait son audio amputé, sans rien pour le
            # dire. Attendre, ici, remonte la pression jusqu'au client par TCP —
            # c'est le comportement correct.
            #
            # Le délai borne le pire cas : si le classement cale pour de bon, on
            # préfère perdre une fenêtre que bloquer la session entière.
            try:
                await asyncio.wait_for(st.queue.put((offset, fenetre)), timeout=5.0)
            except asyncio.TimeoutError:
                st.dropped += 1  # jamais en silence : `partial` le dira en base

    async def _drain_windows(self, st) -> None:
        """Classe les fenêtres une par une, hors de la boucle de réception.

        Classer l'épisode ENTIER d'un coup prendrait ~720 ms sous un seul
        verrou pour trois minutes d'audio, en bloquant toute autre session. Ici
        chaque fenêtre prend ~3 ms de verrou, relâché entre chaque — 0,6 % du
        thread unique par épisode.
        """
        while True:
            item = await st.queue.get()
            if item is None:
                return
            offset, fenetre = item
            try:
                r = await self.classifier.classify_async(fenetre)
            except Exception:  # noqa: BLE001
                log.exception("classification d'une fenêtre d'épisode")
                st.dropped += 1
                continue
            st.scores.append(r.noisy_score)
            st.barks.append(r.bark_score or 0.0)
            st.offsets.append(offset)

    async def _stream_watchdog(self, st) -> None:
        s = self.settings
        while not st.clos:
            await asyncio.sleep(1.0)
            if st.clos:
                return
            maintenant = time.monotonic()

            if (maintenant - st.derniere_trame) * 1000 > s.stream_idle_ms:
                # Le client est mort sans le dire (redémarrage audio, onglet
                # fermé). C'est ce délai qui rattrape le cas.
                await self._finalize_stream(st, "idle")
                return

            if st.duree_ms >= s.max_stream_ms:
                await self._send(P.stream_stop(seq=st.seq, reason=P.REASON_MAX_DURATION))
                await self._finalize_stream(st, P.REASON_MAX_DURATION)
                return

            if (maintenant - st.dernier_bruyant) * 1000 > s.stream_silence_ms:
                await self._send(P.stream_stop(seq=st.seq, reason="silence"))
                await self._finalize_stream(st, "silence")
                return

            if maintenant - st.dernier_progres >= 5:
                st.dernier_progres = maintenant
                await self._send(
                    P.stream_progress(
                        seq=st.seq,
                        received_ms=st.duree_ms,
                        windows=len(st.scores),
                        dropped=st.dropped,
                    )
                )

    async def _on_stream_end(self, payload: dict) -> None:
        if self._discard_stream:
            self._discard_stream = False
            return
        st = self._stream
        if st is None:
            # `stream_end` orphelin : pas une panne, juste un client qui a
            # abandonné un épisode déjà finalisé de notre côté.
            return
        raison = payload.get("stopped_reason") or "client_final"
        await self._finalize_stream(st, raison)

    async def _abort_stream(self, code: str, message: str) -> None:
        """Jette l'épisode : borne de sécurité franchie, ou morceau malformé."""
        st = self._stream
        if st is None:
            return
        st.clos = True
        self._stream = None
        self._flux_clos_a = time.monotonic()
        self._stats["errors"] += 1
        if st.task:
            st.task.cancel()
        if st.watchdog:
            st.watchdog.cancel()
        st.writer.discard()
        log.warning("épisode %s abandonné : %s", st.seq, message)
        await self._fail(code, message, st.seq)

    async def _finalize_stream(self, st, raison: str) -> None:
        """Décide du sort de l'épisode, et le dit au client.

        Règle stricte : un `stream_ack` accepté reçoit exactement UN
        `segment_result` ou UN `error`, jamais zéro ni deux. C'est ce qui permet
        au client de libérer son `inflight[seq]` sans ambiguïté.
        """
        if st.clos:
            return
        st.clos = True
        if self._stream is st:
            self._stream = None
        self._flux_clos_a = time.monotonic()
        # Même précaution que dans `_finalize_listen` : le watchdog appelle
        # `_finalize_stream` sur trois de ses sorties, et s'annuler soi-même
        # ferait lever un CancelledError au milieu de la finalisation. Le code
        # y survivait par accident — un `except (TimeoutError, CancelledError)`
        # plus bas avalait sa propre annulation — ce qui marchait, mais faisait
        # dépendre la survie d'un épisode d'un `except` trop large.
        if st.watchdog and st.watchdog is not asyncio.current_task():
            st.watchdog.cancel()

        # Laisser la file se vider AVANT de juger : sinon on déciderait sur les
        # seules fenêtres déjà classées et on jetterait de vrais événements.
        if st.task:
            try:
                st.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(st.task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                st.task.cancel()
                log.warning("épisode %s : classement non terminé", st.seq)

        # Un épisode clos sans UNE SEULE fenêtre classée est la signature d'une
        # panne silencieuse : le client a ouvert un flux et n'a jamais envoyé de
        # son. C'est exactement ce que produit un `app.js` neuf face à un
        # `recorder-worklet.js` resté dans le cache du CDN — le worklet reçoit
        # `stream_begin`, ne le connaît pas, l'ignore, et n'émet rien. Sans ce
        # message, on ne voit qu'un refus de plus.
        if not st.scores:
            log.warning(
                "épisode %s clos avec %d morceau(x) et %d échantillon(s) mais AUCUNE "
                "fenêtre classée — le client n'a rien envoyé d'exploitable",
                st.seq,
                st.chunks,
                st.samples,
            )

        # Ce que le client a RÉELLEMENT envoyé, avant tout rognage : c'est le
        # seul moyen d'entendre la scène telle qu'elle est arrivée.
        if self.settings.debug_dump and st.samples:
            try:
                debugdump.dump(
                    st.writer.read_range(0, st.samples),
                    f"ep{st.seq}_{st.chunks}morceaux_{st.samples // st.sample_rate}s_{raison}",
                    self.settings.debug_dump_dir,
                    self.settings.debug_dump,
                    self.settings.debug_dump_keep,
                )
            except Exception:  # noqa: BLE001
                log.warning("dump de l'épisode %s impossible", st.seq)

        seuil = self.settings.noisy_threshold
        bornes = st.bornes_retenues(seuil)

        if bornes is None:
            # Mode dev : on conserve TOUTES les captures pour réécoute et analyse !
            # 1. Écriture du WAV dans export/
            horodate = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
            wav_nom = f"{horodate}_ep{st.seq}_refuse.wav"
            x_all = None
            try:
                x_all = st.writer.read_range(0, st.samples)
            except Exception:
                log.exception("relecture de l'épisode refusé %s", st.seq)

            if x_all is not None and x_all.size > 0:
                try:
                    wav_chemin = self.settings.ondemand_dir / wav_nom
                    wav_chemin.write_bytes(wav_bytes(x_all, TARGET_SR))
                except Exception:
                    log.warning("écriture du WAV refusé impossible : %s", wav_nom)
                    wav_nom = None

            # 2. Stockage en base et encodage MP3
            resultat = None
            if x_all is not None and x_all.size > 0:
                try:
                    resultat = await self._store_episode(
                        st,
                        x_all,
                        0,
                        st.samples,
                        raison,
                        seuil,
                        origine="refused",
                        wav_name=wav_nom,
                        evenements_force=0,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception("stockage de l'épisode refusé %s : %r", st.seq, exc)

            if self.settings.save_rejected and not resultat:
                await self._save_rejected_episode(st)
            st.writer.discard()
            self._stats["rejected"] += 1

            await self._send(
                P.segment_result(
                    seq=st.seq,
                    event_id=resultat["event_id"] if resultat else None,
                    accepted=False,
                    result=Bilan(
                        noisy_score=max(st.scores) if st.scores else 0.0,
                        bark_score=max(st.barks) if st.barks else None,
                        mean_noisy_score=(
                            sum(st.scores) / len(st.scores) if st.scores else 0.0
                        ),
                    ),
                    threshold=seuil,
                    duration_ms=resultat["duration_ms"] if resultat else st.duree_ms,
                    mp3_url=resultat["mp3_url"] if resultat else None,
                    mp3_bytes=resultat["mp3_bytes"] if resultat else None,
                    reason=P.REASON_BELOW,
                    noisy_count=0,
                    window_count=len(st.scores),
                )
            )
            return

        premier, dernier = _rogner(st, bornes)

        try:
            x = st.writer.read_range(premier, dernier)
        except OSError as exc:
            log.exception("relecture du temporaire de l'épisode %s", st.seq)
            st.writer.discard()
            await self._fail(P.ERR_INTERNAL, f"relecture : {exc}", st.seq)
            return

        horodate = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
        wav_nom = f"{horodate}_ep{st.seq}_capture.wav"
        wav_chemin = None
        try:
            wav_chemin = self.settings.ondemand_dir / wav_nom
            wav_chemin.write_bytes(wav_bytes(x, TARGET_SR))
        except Exception:
            log.warning("écriture du WAV accepté impossible : %s", wav_nom)
            wav_chemin = None
            wav_nom = None

        # Quality Control (QC) acoustique
        qc_score = None
        qc_valid = None
        origine = "episode"
        evenements_force = None
        accepted_status = True
        reason_status = P.REASON_OK

        if wav_chemin is not None and self.settings.qc_enabled:
            try:
                qc_res = await qc_client.verify_wav(str(wav_chemin))
                if qc_res is not None:
                    qc_score = qc_res.get("score")
                    qc_pct = qc_res.get("percentage", 0.0)
                    duration_ms = round(max(1, x.size) / st.sample_rate * 1000)
                    grid = await qc_client.get_grid()
                    req_thresh = qc_client.get_required_threshold(duration_ms, grid)
                    if qc_pct < req_thresh:
                        qc_valid = False
                        log.info(
                            "Épisode seq=%s rejeté par QC : score=%.1f%% < seuil=%.1f%% (durée=%dms)",
                            st.seq, qc_pct, req_thresh, duration_ms,
                        )
                        origine = "refused"
                        evenements_force = 0
                        accepted_status = False
                        reason_status = P.REASON_BELOW
                    else:
                        qc_valid = True
                        log.info(
                            "Épisode seq=%s validé par QC : score=%.1f%% >= seuil=%.1f%% (durée=%dms)",
                            st.seq, qc_pct, req_thresh, duration_ms,
                        )
            except Exception as qc_err:
                log.warning("Erreur lors de la vérification QC pour seq=%s : %r", st.seq, qc_err)

        try:
            resultat = await self._store_episode(
                st,
                x,
                premier,
                dernier,
                raison,
                seuil,
                origine=origine,
                wav_name=wav_nom,
                evenements_force=evenements_force,
                qc_score=qc_score,
                qc_valid=qc_valid,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("stockage de l'épisode %s", st.seq)
            st.writer.discard()
            await self._fail(P.ERR_INTERNAL, f"stockage : {exc!r}", st.seq)
            return
        finally:
            st.writer.discard()

        # C'est ICI, et pas dans `_store_episode`, que l'on décide de parler au
        # poste : le chemin d'écoute stocke le même épisode sans rien lui dire.
        if resultat is not None:
            await self._send(
                P.segment_result(
                    seq=st.seq,
                    event_id=resultat["event_id"],
                    accepted=accepted_status,
                    result=resultat["bilan"],
                    threshold=seuil,
                    duration_ms=resultat["duration_ms"],
                    mp3_url=resultat["mp3_url"],
                    mp3_bytes=resultat["mp3_bytes"],
                    reason=reason_status,
                    noisy_count=resultat["événements"],
                    partial=resultat["partial"],
                    stopped_reason=raison,
                    window_count=resultat["windows"],
                )
            )

    async def _save_rejected_episode(self, st) -> None:
        """Garde le début d'un épisode refusé, en WAV, hors de `/media`.

        WAV et non MP3, comme pour les segments : ce corpus sert à mesurer des
        scores, et une seconde compression lossy fausserait la comparaison.
        """
        try:
            n = min(st.samples, int(self.settings.save_rejected_max_ms * st.sample_rate / 1000))
            x = st.writer.read_range(0, n)
            relpath = media.rejected_relpath(st.premier_instant(), st.seq, self.settings.app_tz)
            data = wav_bytes(x, TARGET_SR)
            media.write_bytes(self.settings.media_dir, relpath, data)
        except Exception:  # noqa: BLE001
            # Un corpus de calibration raté ne doit pas faire échouer l'épisode :
            # le refus, lui, est déjà décidé.
            log.exception("épisode refusé non conservé (seq=%s)", st.seq)

    async def _store_episode(
        self,
        st,
        x,
        premier: int,
        dernier: int,
        raison: str,
        seuil: float,
        *,
        origine: str = "episode",
        wav_name: str | None = None,
        evenements_force: int | None = None,
        qc_score: float | None = None,
        qc_valid: bool | None = None,
    ) -> dict | None:
        """Stocke l'audio rogné comme un épisode. Rend le verdict, ou None si
        `(client_id, seq)` était déjà en base.

        `origine` ne change QUE la colonne `backend` (`episode/...` ou
        `ondemand/...`) : c'est une étiquette de provenance, jamais filtrée par
        une requête, et elle rend l'archive lisible quand on se demandera d'où
        sort une ligne.

        Ne parle PAS au poste de terrain — c'est l'appelant qui décide.
        `_finalize_stream` lui envoie un `segment_result`, `_finalize_listen`
        n'envoie rien : le poste n'a pas ouvert d'écoute, il n'attend aucun
        accusé, et lui en envoyer un ferait apparaître « seq N accepté » sur le
        kiosque pour une action de l'opérateur, tout en laissant un
        `inflight[seq]` orphelin.
        """
        s = self.settings
        detected_at = st.premier_instant() + timedelta(
            milliseconds=premier / st.sample_rate * 1000
        )
        duration_ms = round(max(1, x.size) / st.sample_rate * 1000)
        recu = datetime.now(timezone.utc)

        event_id = await db.fetchval("SELECT nextval('events_id_seq')")
        relpath = media.mp3_relpath(detected_at, event_id, s.app_tz)
        mp3 = encode_mp3(x, TARGET_SR)
        mp3_bytes = media.write_bytes(s.media_dir, relpath, mp3)

        bilan = Bilan(
            noisy_score=max(st.scores) if st.scores else 0.0,
            bark_score=max(st.barks) if st.barks else None,
            mean_noisy_score=sum(st.scores) / len(st.scores) if st.scores else 0.0,
        )
        événements = (
            evenements_force
            if evenements_force is not None
            else compter_evenements(st.scores, st.offsets, seuil, st.sample_rate)
        )

        row = await db.fetchrow(
            EPISODE_INSERT_SQL,
            event_id,
            detected_at,
            recu,
            _captured_at_utc(None),
            self.client_id,
            st.seq,
            bilan.noisy_score,
            bilan.bark_score,
            bilan.mean_noisy_score,
            duration_ms,
            st.sample_rate,
            relpath,
            mp3_bytes,
            f"{origine}/{raison}",
            None,
            None,
            événements,
            st.dropped > 0,
            raison,
            len(st.scores),
            wav_name,
            qc_score,
            qc_valid,
        )
        if row is None:
            # Déjà stocké sous ce (client_id, seq) : on jette ce qu'on vient
            # d'écrire plutôt que de laisser un doublon sur disque.
            media.delete(s.media_dir, relpath)
            return None

        if origine != "refused":
            self._stats["accepted"] += 1
        return {
            "event_id": event_id,
            "bilan": bilan,
            "mp3_url": media.url_for(relpath),
            "mp3_bytes": mp3_bytes,
            "duration_ms": duration_ms,
            "événements": événements,
            "partial": st.dropped > 0,
            "windows": len(st.scores),
            "detected_at": detected_at,
            "wav_name": wav_name,
            "qc_score": qc_score,
            "qc_valid": qc_valid,
        }

    # ------------------------------------------------- écoute à la demande

    async def request_listen(self, duration_ms: int | None = None) -> tuple[bool, str | None]:
        """Ouvre une écoute pour un opérateur. Rend `(ok, message d'échec)`.

        Appelé par la session opérateur, jamais par le poste. On ATTEND
        l'acquittement du poste : sans lui, l'opérateur ne pourrait pas
        distinguer « le poste n'a pas reçu l'ordre » de « le micro est muet »,
        et chercherait une panne réseau pendant une heure.
        """
        s = self.settings
        if not (s.ondemand_enabled and s.client_listen_enabled):
            return False, "l'écoute à la demande est désactivée sur ce serveur"
        if self._listen is not None:
            return False, P.ERR_LISTENING

        # Un épisode en cours n'est PAS un motif de refus : l'écoute l'INTERROMPT.
        #
        # C'est le CLIENT qui séquence les deux, et c'est là toute l'astuce :
        # il tient les deux états, donc il clôt son épisode et ouvre l'écoute
        # dans le MÊME tick. Le serveur reçoit `stream_end` puis `listen_start`
        # dans cet ordre, sur le même socket, et sa boucle de réception est
        # strictement séquentielle — aucun échantillon ne peut se glisser entre
        # les deux. Le serveur n'a donc rien à ordonner, et surtout rien à
        # couper : l'épisode est jugé et archivé normalement sur l'audio reçu
        # jusque-là.
        #
        # Refuser serait le pire des deux mondes : c'est quand ça détecte qu'on a
        # envie d'écouter, et sur un terrain actif un épisode est ouvert la
        # quasi-totalité du temps.
        #
        # D'où le délai plus large : il faut d'abord que l'épisode se finalise
        # (vidage de la file de classement) avant que le poste n'accuse
        # réception de l'écoute.
        attente = LISTEN_ACK_TIMEOUT_S * 4 if self._stream is not None else LISTEN_ACK_TIMEOUT_S

        duree = int(duration_ms or s.listen_duration_ms)
        duree = max(1_000, min(duree, s.listen_max_ms))
        listen_id = uuid.uuid4().hex
        self._listen_demande = (listen_id, duree, s.listen_chunk_ms)
        self._listen_ack = asyncio.get_running_loop().create_future()

        try:
            await self._send(
                P.listen_request(
                    listen_id=listen_id,
                    duration_ms=duree,
                    sample_rate=TARGET_SR,
                    chunk_ms=s.listen_chunk_ms,
                    max_chunks=s.listen_max_chunks,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._resolve_listen_ack(False, f"envoi de l'ordre impossible : {exc!r}")
            return False, "le poste n'a pas pu être commandé"

        try:
            return await asyncio.wait_for(self._listen_ack, timeout=attente)
        except asyncio.TimeoutError:
            self._listen_demande = None
            self._resolve_listen_ack(False, "délai dépassé")
            # Les causes possibles, nommées : sans ça, l'opérateur voit « aucun
            # son » et va chercher un bug réseau là où il n'y en a pas.
            return False, (
                f"aucune réponse du poste en {int(attente)} s — page pas à jour "
                "(worklet ancien), page arrêtée, ou poste déconnecté"
            )

    async def stop_listen(self, raison: str = P.REASON_LISTEN_OPERATOR) -> None:
        """Interrompt l'écoute en cours, à la demande de l'opérateur.

        On prévient le poste AVANT de finaliser : il cessera d'envoyer, et les
        morceaux déjà en vol seront absorbés par `_discard_listen`. Sans cet
        avis, le poste continuerait une minute pour rien.
        """
        st = self._listen
        if st is None:
            return
        info = self._listen_info
        if info is not None:
            await self._send(P.listen_stop(listen_id=info.listen_id, reason=raison))
        self._discard_listen = True
        await self._finalize_listen(st, raison)

    def _resolve_listen_ack(self, ok: bool, message: str | None) -> None:
        """Résout l'attente de l'opérateur, une seule fois."""
        fut = self._listen_ack
        self._listen_ack = None
        if fut is not None and not fut.done():
            fut.set_result((ok, message))

    async def _on_listen_start(self, payload: dict) -> None:
        s = self.settings
        try:
            entete = ListenStart.model_validate(payload)
        except ValidationError as exc:
            await self._reject_listen(P.ERR_BAD_JSON, f"listen_start malformé : {exc.errors()}")
            return

        attendu = self._listen_demande
        if attendu is None or entete.listen_id != attendu[0]:
            # Accepter ouvrirait une écoute que personne n'écoute, et le WAV
            # serait écrit sans que quiconque l'ait demandé.
            await self._reject_listen(
                P.ERR_LISTEN_UNKNOWN,
                f"listen_start {entete.listen_id!r} sans demande correspondante",
            )
            return

        if entete.refus:
            # Le poste a reçu l'ordre et le refuse, en disant pourquoi. On
            # remonte SON motif, pas un message générique : c'est la différence
            # entre « worklet pas à jour, rechargez la page » et une heure
            # passée à chercher une panne réseau.
            await self._reject_listen(P.ERR_LISTEN_REFUSED, entete.refus)
            return

        if entete.format != "s16le":
            await self._reject_listen(P.ERR_BAD_FORMAT, f"format {entete.format!r}")
            return
        if entete.channels != 1:
            await self._reject_listen(P.ERR_BAD_FORMAT, f"{entete.channels} canaux")
            return
        # Même exigence que l'épisode, et pour la même raison : soxr est
        # stateful, l'appeler morceau par morceau introduirait un clic par
        # frontière. Le client est déjà à 16 kHz, ce refus ne devrait jamais
        # se déclencher — c'est un filet contre un poste non rechargé.
        if entete.sample_rate != TARGET_SR:
            await self._reject_listen(
                P.ERR_BAD_SAMPLE_RATE,
                f"une écoute exige {TARGET_SR} Hz, reçu {entete.sample_rate}",
            )
            return

        listen_id, duree, chunk_ms = attendu
        maintenant = time.monotonic()
        st = StreamState(
            seq=entete.seq,
            sample_rate=entete.sample_rate,
            # PAS de pré-roll sur ce chemin. Une seconde de pré-roll donnerait
            # un fichier dont la tête précède l'horodatage de son propre nom —
            # un petit mensonge gratuit sur l'axe du temps, pour une prise de
            # son dont l'intérêt est justement d'être datée.
            pre_roll_samples=0,
            received_at=datetime.now(timezone.utc),
            writer=ondemand.WavSpool(
                s.ondemand_dir,
                ondemand.sample_name(datetime.now(timezone.utc), s.app_tz, duree / 1000),
                entete.sample_rate,
            ),
            scorer=WindowScorer(),
        )
        st.derniere_trame = maintenant
        st.dernier_bruyant = maintenant
        st.dernier_progres = maintenant
        st.queue = asyncio.Queue(maxsize=FILE_FENETRES)
        st.task = asyncio.create_task(self._drain_windows(st))
        st.watchdog = asyncio.create_task(self._listen_watchdog(st))
        self._tasks.add(st.task)
        self._tasks.add(st.watchdog)

        self._listen = st
        self._listen_demande = None
        info = ListenInfo(
            listen_id=listen_id,
            duration_ms=duree,
            chunk_ms=chunk_ms,
            sample_rate=entete.sample_rate,
            owner=self,
        )
        self._listen_info = info

        # L'ordre compte : on déclare l'écoute au hub, PUIS on diffuse
        # `listen_started`. La diffusion passe par la même file que l'audio,
        # donc elle part avant le premier morceau par construction — un
        # `AudioBufferSourceNode` qui recevrait du son avant de connaître la
        # fréquence ne saurait pas le jouer.
        if self.hub is not None:
            self.hub.open_listen(info)
            self.hub.broadcast(
                P.listen_started(
                    listen_id=listen_id,
                    duration_ms=duree,
                    chunk_ms=chunk_ms,
                    remaining_ms=duree,
                    sample_rate=entete.sample_rate,
                    joined=False,
                )
            )
        log.info(
            "écoute %s ouverte (seq=%s, %d s, %d ms/morceau)",
            listen_id,
            st.seq,
            duree // 1000,
            chunk_ms,
        )
        self._resolve_listen_ack(True, None)

    async def _on_listen_chunk(self, data: bytes) -> None:
        st = self._listen
        s = self.settings
        st.derniere_trame = time.monotonic()
        st.chunks += 1
        st.bytes_recus += len(data)

        if len(data) % 2 or len(data) > s.stream_chunk_max_bytes:
            await self._abort_listen(
                P.ERR_BAD_LENGTH,
                f"morceau de {len(data)} octets (limite {s.stream_chunk_max_bytes}, paire exigée)",
            )
            return

        # Bornes DÉDIÉES, et non celles de l'épisode : à 200 ms par morceau,
        # 60 s en font 300, alors que `max_stream_chunks` vaut 256. Les
        # confondre ferait s'arrêter l'écoute à 51 s, avec pour seul symptôme
        # une durée trop courte — sans aucun rapport lisible avec la cause.
        if st.bytes_recus > s.listen_max_bytes or st.chunks > s.listen_max_chunks:
            await self._abort_listen(
                P.ERR_PAYLOAD_TOO_LARGE,
                f"écoute hors bornes : {st.bytes_recus} octets, {st.chunks} morceaux",
            )
            return

        # ① DIFFUSER D'ABORD. Avant le disque, avant le classifieur.
        #
        # C'est cette ligne qui rend l'écoute directe indépendante du disque et
        # du modèle : une réanalyse de 320 ms lancée depuis le panneau décalera
        # des scores, jamais le son. Si quelqu'un « optimise » un jour en
        # faisant passer le PCM par la file de classification, il réintroduit
        # la panne — un dump de trois minutes ferait un trou d'une seconde dans
        # le direct. Ne pas déplacer cette ligne.
        if self.hub is not None:
            self.hub.publish(data)

        st.writer.write(data)
        st.samples += len(data) // 2
        x = pcm16_to_float32(data, channels=1)

        # `put_nowait` et JAMAIS `await queue.put(...)`, à la différence du
        # chemin épisode. Là-bas on attend une place parce que jeter une
        # fenêtre AMPUTE le fichier, qui est rogné sur les fenêtres retenues.
        # Ici le WAV est complet par construction : une fenêtre perdue ne coûte
        # qu'un score, et le panneau sait refaire l'analyse proprement sur le
        # fichier final. Un `await` ici insérerait un trou dans l'audio que
        # l'opérateur est en train d'écouter.
        for offset, fenetre in st.scorer.feed(x):
            try:
                st.queue.put_nowait((offset, fenetre))
            except asyncio.QueueFull:
                st.dropped += 1

    async def _on_listen_end(self, payload: dict) -> None:
        # Le drapeau tombe ici dans tous les cas : c'est la fin du flux, donc
        # plus aucun morceau d'écoute ne peut suivre sans un nouvel ordre.
        self._discard_listen = False
        st = self._listen
        if st is None:
            # Fin orpheline : écoute déjà finalisée de notre côté. Pas une panne.
            return
        try:
            fin = ListenEnd.model_validate(payload)
        except ValidationError:
            await self._finalize_listen(st, P.REASON_LISTEN_CLIENT)
            return
        info = self._listen_info
        if info is not None and fin.listen_id != info.listen_id:
            # Un `listen_end` qui désigne une AUTRE écoute ne clôt pas celle-ci.
            return
        await self._finalize_listen(st, fin.stopped_reason or P.REASON_LISTEN_CLIENT)

    async def _reject_listen(self, code: str, message: str) -> None:
        """Refuse une écoute à l'ouverture et avale tout ce qui suit.

        On n'ouvre AUCUN état : le poste va envoyer ses morceaux quand même, et
        c'est `_discard_listen` qui les absorbe jusqu'à `listen_end`.
        """
        self._discard_listen = True
        self._listen_demande = None
        self._resolve_listen_ack(False, message)
        await self._fail(code, message)

    async def _abort_listen(self, code: str, message: str) -> None:
        """Interrompt une écoute sur une borne de sécurité ou un morceau malformé.

        Le WAV est PUBLIÉ quand même : c'est une vraie prise de son, et la
        seule copie. On ne fait plus confiance aux scores, donc pas d'épisode —
        mais on ne détruit pas l'audio.
        """
        st = self._listen
        if st is None:
            return
        self._discard_listen = True
        self._stats["errors"] += 1
        log.warning("écoute %s interrompue : %s", st.seq, message)
        await self._finalize_listen(st, P.REASON_LISTEN_ABORTED)
        await self._fail(code, message, st.seq)

    async def _listen_watchdog(self, st) -> None:
        """Veille sur une écoute. DEUX sorties, et surtout pas trois.

        `_stream_watchdog` en a une troisième — le silence — qui tuerait une
        écoute de 60 s sur un champ calme à la trentième seconde. C'est-à-dire
        exactement le cas d'usage : on écoute dehors pour savoir ce qui s'y
        passe, et « rien » est une réponse parfaitement valide.
        """
        s = self.settings
        while not st.clos:
            await asyncio.sleep(1.0)
            if st.clos:
                return
            maintenant = time.monotonic()

            if (maintenant - st.derniere_trame) * 1000 > s.listen_idle_ms:
                # Le poste est mort sans le dire : redémarrage audio, onglet
                # fermé, machine éteinte.
                await self._finalize_listen(st, P.REASON_LISTEN_IDLE)
                return

            info = self._listen_info
            if info is not None and st.duree_ms >= info.duration_ms:
                await self._send(
                    P.listen_stop(listen_id=info.listen_id, reason=P.REASON_LISTEN_DURATION)
                )
                await self._finalize_listen(st, P.REASON_LISTEN_DURATION)
                return

            # Filet : le poste ignore `duration_ms` (client ancien) ou son
            # horloge d'échantillons dérive. On clôt de notre chef.
            if st.duree_ms >= s.listen_max_ms:
                await self._finalize_listen(st, P.REASON_LISTEN_DURATION)
                return

            if maintenant - st.dernier_progres >= 2:
                st.dernier_progres = maintenant
                if self.hub is not None:
                    self.hub.broadcast(
                        P.listen_progress(
                            received_ms=st.duree_ms,
                            windows=len(st.scores),
                            max_noisy_score=max(st.scores) if st.scores else None,
                            dropped_chunks=self.hub.dropped_chunks,
                            dropped_windows=st.dropped,
                        )
                    )

    async def _finalize_listen(self, st, raison: str) -> None:
        """Publie le WAV, juge, archive, et prévient l'opérateur.

        L'ORDRE DES ÉTAPES EST LA GARANTIE : le WAV est publié AVANT tout
        jugement. Même si le classement, la base ou l'écriture de l'index
        échouent, le fichier est sur disque et complet.
        """
        if st.clos:
            return
        st.clos = True
        if self._listen is st:
            self._listen = None
        self._listen_clos_a = time.monotonic()
        info = self._listen_info
        self._listen_info = None
        # Ne PAS annuler le watchdog s'il est la tâche courante : `_finalize_listen`
        # est justement appelé depuis lui sur les sorties idle/durée, et
        # `cancel()` sur soi-même jette un CancelledError au prochain `await` —
        # c'est-à-dire au milieu de la publication du WAV.
        if st.watchdog and st.watchdog is not asyncio.current_task():
            st.watchdog.cancel()
        if self.hub is not None:
            self.hub.close_listen()

        # Laisser la file se vider AVANT de juger, comme pour un épisode : sans
        # ça on déciderait sur les seules fenêtres déjà classées.
        if st.task:
            try:
                st.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(st.task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                st.task.cancel()
                log.warning("écoute %s : classement non terminé", st.seq)

        # ① PUBLIER LE WAV. Toujours. Avant tout le reste.
        chemin = st.writer.finalize()
        nom = chemin.name if chemin is not None else None

        seuil = self.settings.noisy_threshold
        resultat = None
        bornes = st.bornes_retenues(seuil) if chemin is not None else None

        if bornes is not None:
            premier, dernier = _rogner(st, bornes)
            try:
                x = st.writer.read_range(premier, dernier)
            except OSError as exc:
                log.exception("relecture du WAV d'écoute %s", st.seq)
                x = None
            if x is not None and x.size:
                qc_score = None
                qc_valid = None
                if chemin is not None and self.settings.qc_enabled:
                    try:
                        dur_ms = round(max(1, x.size) / st.sample_rate * 1000)
                        qc_score, qc_valid = await qc_client.evaluate_qc(str(chemin), dur_ms)
                    except Exception:
                        pass
                try:
                    resultat = await self._store_episode(
                        st,
                        x,
                        premier,
                        dernier,
                        raison,
                        seuil,
                        origine="ondemand",
                        wav_name=nom,
                        qc_score=qc_score,
                        qc_valid=qc_valid,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("stockage de l'écoute %s", st.seq)

        # ② L'analyse, portée au sidecar pour que le panneau sache ce qui a déjà
        #    été soumis à YAMNet et avec quel seuil.
        if chemin is not None:
            self._ecrire_analyse(st, chemin, raison, seuil, resultat)

        log.info(
            "écoute %s close (%s) — %s, %.1f s, %d fenêtre(s), %s",
            info.listen_id if info else "?",
            raison,
            nom or "aucun fichier",
            st.samples / st.sample_rate,
            len(st.scores),
            "épisode archivé" if resultat else "pas d'épisode",
        )

        if self.hub is not None:
            self.hub.broadcast(
                P.listen_ended(
                    listen_id=info.listen_id if info else "",
                    reason=raison,
                    wav_name=nom,
                    duration_ms=st.duree_ms,
                    windows=len(st.scores),
                    # Le débit de l'auditeur n'a AUCUN effet sur l'enregistrement
                    # (le WAV est complet) — seulement sur ce qu'il a entendu.
                    # Il faut donc le lui dire : sinon un hoquet réseau se lit
                    # comme une accalmie dehors.
                    dropped_chunks=self.hub.dropped_chunks,
                    dropped_windows=st.dropped,
                    partial=st.dropped > 0,
                    analysis=self._bilan_dict(st, seuil, resultat),
                    event_id=resultat["event_id"] if resultat else None,
                    mp3_url=resultat["mp3_url"] if resultat else None,
                )
            )

    def _bilan_dict(self, st, seuil: float, resultat: dict | None) -> dict:
        """Ce que le panneau affiche sous le lecteur.

        La DENSITÉ (fenêtres retenues / fenêtres totales) est là et pas
        seulement le score max : une écoute rognée sur [première fenêtre
        retenue, dernière] peut faire 55 s contenant un événement à la seconde 3
        et un autre à la 58, avec 55 s de vent au milieu. Un « 0,81 » seul
        laisserait croire à une scène bruyante.
        """
        retenues = sum(1 for s in st.scores if s >= seuil)
        return {
            "noisy_score": max(st.scores) if st.scores else None,
            "bark_score": max(st.barks) if st.barks else None,
            "mean_noisy_score": (sum(st.scores) / len(st.scores)) if st.scores else None,
            "threshold": seuil,
            "windows": len(st.scores),
            "windows_retenues": retenues,
            "scores": [
                {"offset_ms": round(o / st.sample_rate * 1000), "noisy": round(s, 6)}
                for o, s in zip(st.offsets, st.scores)
            ],
            "event_id": resultat["event_id"] if resultat else None,
            "mp3_url": resultat["mp3_url"] if resultat else None,
            "qc_score": resultat.get("qc_score") if resultat else None,
            "qc_valid": resultat.get("qc_valid") if resultat else None,
        }

    def _ecrire_analyse(self, st, chemin, raison: str, seuil: float, resultat: dict | None) -> None:
        """Écrit l'entrée d'index du sample. Ne lève jamais.

        Un index raté ne doit pas empêcher l'annonce d'une écoute réussie : le
        WAV est sur disque, c'est lui qui compte.
        """
        try:
            s = self.settings
            idx = ondemand.AnalysisIndex(s.ondemand_dir)
            digest = ondemand.sha256_file(chemin)
            bilan = self._bilan_dict(st, seuil, resultat)
            info = self.classifier.describe() if self.classifier is not None else {}
            idx.put(
                digest,
                {
                    "name": chemin.name,
                    "bytes": chemin.stat().st_size,
                    "origine": "listen",
                    "stopped_reason": raison,
                    "partial": st.dropped > 0,
                    "sample_rate": st.sample_rate,
                    "duration_ms": st.duree_ms,
                    "threshold": seuil,
                    "backend": info.get("backend"),
                    "model": info.get("model"),
                    **bilan,
                },
            )
        except Exception:  # noqa: BLE001
            log.warning("index d'analyse non écrit pour %s", chemin, exc_info=True)

    async def _store(
        self,
        seg: SegmentStart,
        result,
        x16,
        detected_at: datetime,
        received_at: datetime,
        duration_ms: int,
    ) -> tuple[int, str, int | None, bool]:
        s = self.settings

        # Réservation de l'identifiant AVANT l'écriture : le nom du fichier en
        # dépend. Deux nextval concurrents ne se marchent jamais dessus, et un
        # numéro brûlé sur un doublon n'a aucune importance pour un BIGSERIAL.
        event_id = await db.fetchval("SELECT nextval('events_id_seq')")
        relpath = media.mp3_relpath(detected_at, event_id, s.app_tz)
        mp3 = encode_mp3(x16, TARGET_SR)
        mp3_bytes = media.write_bytes(s.media_dir, relpath, mp3)

        row = await db.fetchrow(
            INSERT_SQL,
            event_id,
            detected_at,
            received_at,
            _captured_at_utc(seg.captured_at_ms),
            self.client_id,
            seg.seq,
            result.noisy_score,
            result.bark_score,
            result.mean_noisy_score,
            duration_ms,
            seg.sample_rate,
            relpath,
            mp3_bytes,
            result.backend,
            result.model_version,
            json.dumps(result.top_dicts()),
        )

        if row is not None:
            log.info(
                "événement %d accepté (seq=%d, score=%.3f, bark=%.3f, %s, %d o)",
                event_id,
                seg.seq,
                result.noisy_score,
                result.bark_score or 0.0,
                relpath,
                mp3_bytes,
            )
            return event_id, relpath, mp3_bytes, False

        # Conflit : ce seq a déjà été enregistré — le client a réémis après une
        # coupure socket dont il n'a pas vu l'acquittement. On jette le fichier
        # qu'on vient d'écrire et on ré-acquitte l'événement existant, pour
        # qu'un retry ne duplique ni la ligne ni le MP3.
        media.delete(s.media_dir, relpath)
        existing = await db.fetchrow(SELECT_EXISTING_SQL, self.client_id, seg.seq)
        if existing is None:
            raise RuntimeError(
                f"conflit sur (client_id={self.client_id}, seq={seg.seq}) sans ligne "
                "existante — index unique incohérent ?"
            )
        log.info(
            "segment seq=%d déjà enregistré (événement %d) — ré-acquittement",
            seg.seq,
            existing["id"],
        )
        return existing["id"], existing["mp3_path"], existing["mp3_bytes"], True

    async def _save_rejected(self, seg: SegmentStart, x16, detected_at: datetime) -> None:
        """Écrit le WAV d'un refus, sous SAVE_REJECTED (§5.2).

        Enregistré à 16 kHz, exactement ce que le classifieur a reçu : rejouer
        le fichier dans le classifieur redonne le score au chiffre près, ce qui
        est précisément ce dont on a besoin pour régler un seuil. Un WAV à la
        fréquence d'origine serait plus agréable à écouter et moins fidèle.
        """
        s = self.settings
        relpath = media.rejected_relpath(detected_at, seg.seq, s.app_tz)
        try:
            media.write_bytes(s.media_dir, relpath, wav_bytes(x16, TARGET_SR))
        except Exception:  # noqa: BLE001
            log.exception("écriture du refus %s", relpath)

    # ------------------------------------------------------------ sortant

    async def _send(self, message: dict) -> None:
        # Verrou : plusieurs tâches de segment peuvent répondre en parallèle.
        async with self._send_lock:
            try:
                await self.ws.send_json(message)
            except Exception:  # noqa: BLE001
                # Client parti. La boucle principale s'en apercevra au prochain
                # receive ; inutile d'inonder le journal.
                pass

    async def _fail(self, code: str, message: str, seq: int | None = None, fatal: bool = False) -> None:
        self._stats["errors"] += 1
        log.warning(
            "erreur %s (seq=%s, fatal=%s) : %s", code, seq, fatal, message
        )
        await self._send(P.error(code, message, seq=seq, fatal=fatal))
        if fatal:
            # On ferme ET on sort de la boucle : une erreur fatale signifie que
            # le client ne se réparera pas en continuant à parler. Le laisser
            # reboucler en reconnexion infinie est exactement ce que le
            # drapeau `fatal` existe pour éviter (§6).
            self._fatal = True
            await self.ws.close(code=1008)

    async def _reject_segment(self, code: str, message: str, seq: int) -> None:
        """Refuse les métadonnées ; la trame binaire qui suit sera avalée."""
        self._discard_next = True
        self._pending_segment = None
        await self._fail(code, message, seq=seq)
