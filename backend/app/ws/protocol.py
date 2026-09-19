"""Messages du protocole WebSocket et constructeurs des réponses serveur.

DEUX FORMES COEXISTENT, et ce n'est pas un luxe :

  segment — [une trame texte `segment_start`][une trame binaire PCM]. Le
            chemin historique, conservé tel quel parce que les .js sont servis
            par un CDN qui les garde quatre heures : un client d'avant doit
            continuer à fonctionner pendant tout ce temps.

  épisode — [`stream_start`][N trames binaires][`stream_end`]. Le flux continu
            qui remplace les clips recollés.

`PROTOCOL_VERSION` NE BOUGE PAS. Sa vérification au hello est FATALE : l'incré-
menter tuerait tous les clients en vol, c'est-à-dire exactement ceux que le
cache sert encore. Le nouveau chemin est purement additif.
"""

from __future__ import annotations

import time
from typing import Any

PROTOCOL_VERSION = 1

# --- types entrants ---
T_HELLO = "hello"
T_SEGMENT_START = "segment_start"
T_STREAM_START = "stream_start"
T_STREAM_END = "stream_end"
T_PING = "ping"
T_LISTEN_START = "listen_start"
T_LISTEN_END = "listen_end"

# --- types sortants ---
T_HELLO_ACK = "hello_ack"
T_SEGMENT_RESULT = "segment_result"
T_STREAM_ACK = "stream_ack"
T_STREAM_PROGRESS = "stream_progress"
T_STREAM_STOP = "stream_stop"
T_ERROR = "error"
T_PONG = "pong"
T_LISTEN_REQUEST = "listen_request"
T_LISTEN_STOP = "listen_stop"

# --- canal opérateur (/ws/listen), non versionné ---
# Un protocole À PART, et pas des types ajoutés à ceux du poste : l'opérateur
# n'est pas un client de capture. Il n'envoie jamais d'audio, il ne fait pas de
# `hello`, et une session d'écoute ne doit pas pouvoir se faire passer pour un
# poste de terrain. Séparer les deux vocabulaires rend l'usurpation structurelle
# plutôt que vérifiée.
T_LISTEN_BEGIN = "listen_begin"
T_LISTEN_CANCEL = "listen_cancel"
T_LISTEN_STARTED = "listen_started"
T_LISTEN_PROGRESS = "listen_progress"
T_LISTEN_ENDED = "listen_ended"

# --- codes d'erreur ---
ERR_BAD_JSON = "bad_json"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_BAD_LENGTH = "bad_length"
ERR_BAD_SAMPLE_RATE = "bad_sample_rate"
ERR_BAD_FORMAT = "bad_format"
ERR_PAYLOAD_TOO_LARGE = "payload_too_large"
ERR_BUSY = "busy"
ERR_DECODE_ERROR = "decode_error"
ERR_CLASSIFY_ERROR = "classify_error"
ERR_INTERNAL = "internal"
# Un épisode est déjà ouvert sur cette session. Un seul à la fois : c'est ce
# qui borne la charge, le garde-tempête du client ne bornant que les
# DÉCLENCHEMENTS (un épisode long n'en produit aucun).
ERR_STREAM_OPEN = "stream_open"
ERR_LOW_DISK = "low_disk"
# Une écoute est en cours sur cette session : un épisode ne peut pas s'ouvrir.
# Ce code existe SÉPARÉMENT de `busy`, et ce n'est pas un détail de nommage :
# le client traite `busy` en remettant le segment en file, or un flux n'a pas
# de PCM à remettre en file — `flushQueue` appellerait `sendSegment()` sur des
# métadonnées et lèverait un TypeError au milieu du gestionnaire.
ERR_LISTENING = "listening"
# L'écoute est refusée parce qu'un épisode est déjà ouvert.
ERR_EPISODE_OPEN = "episode_open"
# Aucun poste de terrain n'est connecté, ou plusieurs le sont. Dans les deux cas
# on refuse en le DISANT : choisir en silence lequel écouter serait
# indiagnosticable, l'opérateur entendrait un autre champ que le sien.
ERR_NO_FIELD = "no_field_client"
ERR_FIELD_AMBIGUOUS = "field_ambiguous"
# Un `listen_start` qui ne correspond à aucune demande en cours : demande
# expirée, client bogué, ou rejeu. On refuse sans ouvrir — accepter ouvrirait
# une écoute que personne n'écoute.
ERR_LISTEN_UNKNOWN = "listen_unknown"
# Le poste a reçu l'ordre et le REFUSE, en disant pourquoi (page arrêtée,
# worklet pas à jour, épisode en cours). Le motif voyage avec le refus : c'est
# ce qui évite à l'opérateur de chercher une panne réseau là où le poste lui a
# déjà répondu.
ERR_LISTEN_REFUSED = "listen_refused"

# --- raisons d'acceptation / de refus, journalisées telles quelles ---
REASON_OK = "noisy_score_ok"
REASON_BELOW = "below_threshold"
REASON_DUPLICATE = "duplicate_seq"
REASON_MAX_DURATION = "max_duration"
# L'épisode a été clos pour laisser passer une écoute à la demande. Le CLIENT
# est seul à émettre cette raison : c'est lui qui clôt son flux quand il reçoit
# un `listen_request` pendant un épisode. Le serveur la reçoit dans
# `stream_end.stopped_reason` et la journalise telle quelle.
REASON_LISTEN_PREEMPT = "listen_preempt"

# --- raisons de fin d'écoute ---
REASON_LISTEN_DURATION = "duration"          # les 60 s demandées sont écoulées
REASON_LISTEN_OPERATOR = "operator_cancel"   # l'opérateur a cliqué « arrêter »
REASON_LISTEN_CLIENT = "client_final"        # le poste a clos de lui-même
REASON_LISTEN_IDLE = "idle"                  # plus rien depuis listen_idle_ms
REASON_LISTEN_ABORTED = "aborted"            # morceau malformé : on garde l'audio


def now_ms() -> int:
    return int(time.time() * 1000)


def error(
    code: str, message: str, seq: int | None = None, fatal: bool = False
) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": T_ERROR, "code": code, "message": message, "fatal": fatal}
    if seq is not None:
        msg["seq"] = seq
    return msg


def hello_ack(
    *,
    session_id: str,
    server_version: str,
    classifier_info: dict,
    limits: dict,
    config_patch: dict,
    client_id: str,
) -> dict[str, Any]:
    return {
        "type": T_HELLO_ACK,
        "protocol_version": PROTOCOL_VERSION,
        "server_version": server_version,
        "session_id": session_id,
        "server_time_ms": now_ms(),
        "client_id": client_id,
        "classifier": classifier_info,
        "limits": limits,
        "config_patch": config_patch,
    }


def segment_result(
    *,
    seq: int,
    event_id: int | None,
    accepted: bool,
    result,
    threshold: float,
    duration_ms: int,
    mp3_url: str | None,
    mp3_bytes: int | None,
    reason: str,
    noisy_count: int | None = None,
    partial: bool = False,
    stopped_reason: str | None = None,
    window_count: int | None = None,
) -> dict[str, Any]:
    msg = {
        "type": T_SEGMENT_RESULT,
        "seq": seq,
        "event_id": event_id,
        "accepted": accepted,
        # Le score principal est celui du GROUPE SURVEILLÉ : c'est lui qui décide.
        # bark_score est joint en diagnostic, pour comparer les deux critères
        # sur des données réelles sans avoir à redéployer.
        "noisy_score": round(result.noisy_score, 6),
        "bark_score": None if result.bark_score is None else round(result.bark_score, 6),
        "mean_noisy_score": round(result.mean_noisy_score, 6),
        "threshold": threshold,
        "top_classes": result.top_classes,
        "duration_ms": duration_ms,
        "mp3_url": mp3_url,
        "mp3_bytes": mp3_bytes,
        "processing_ms": round(result.processing_ms, 1),
        "reason": reason,
        "server_time_ms": now_ms(),
    }
    # Champs d'épisode. Absents sur le chemin segment, donc invisibles pour un
    # client ancien — c'est ce qui permet d'ajouter sans toucher à la version
    # du protocole.
    if noisy_count is not None:
        msg["noisy_count"] = noisy_count
    if partial:
        msg["partial"] = True
    if stopped_reason is not None:
        msg["stopped_reason"] = stopped_reason
    if window_count is not None:
        msg["window_count"] = window_count
    return msg


def pong(t: int | None) -> dict[str, Any]:
    return {"type": T_PONG, "t": t, "server_time_ms": now_ms()}


def stream_ack(
    *,
    seq: int,
    stream_id: str,
    duplicate: bool,
    max_stream_ms: int,
    max_chunk_bytes: int,
    silence_ms: int,
) -> dict[str, Any]:
    """Acquittement IMMÉDIAT, avant le moindre échantillon.

    Sans lui le client resterait aveugle pendant tout l'épisode : il ne saurait
    ni si son audio arrive, ni à quelle durée maximale s'arrêter, ni si le
    serveur a seulement compris qu'un flux commençait.
    """
    return {
        "type": T_STREAM_ACK,
        "seq": seq,
        "stream_id": stream_id,
        "duplicate": duplicate,
        "max_stream_ms": max_stream_ms,
        "max_chunk_bytes": max_chunk_bytes,
        "silence_ms": silence_ms,
        "server_time_ms": now_ms(),
    }


def stream_progress(*, seq: int, received_ms: int, windows: int, dropped: int) -> dict[str, Any]:
    """Preuve de vie, toutes les quelques secondes.

    `dropped` est le seul endroit où une surcharge de classification devient
    visible : sans lui, des scores calculés sur la moitié d'un épisode
    passeraient pour des scores complets.
    """
    return {
        "type": T_STREAM_PROGRESS,
        "seq": seq,
        "received_ms": received_ms,
        "windows": windows,
        "dropped": dropped,
        "server_time_ms": now_ms(),
    }


def stream_stop(*, seq: int, reason: str) -> dict[str, Any]:
    """Le serveur clôt de son propre chef (silence mesuré chez lui, ou durée)."""
    return {"type": T_STREAM_STOP, "seq": seq, "reason": reason, "server_time_ms": now_ms()}


# ------------------------------------------------------- écoute à la demande


def listen_request(
    *,
    listen_id: str,
    duration_ms: int,
    sample_rate: int,
    chunk_ms: int,
    max_chunks: int,
) -> dict[str, Any]:
    """Ordre donné au poste d'ouvrir une écoute.

    `listen_id` est OBLIGATOIREMENT réémis par le client dans son
    `listen_start`, et ce n'est pas une redondance : entre l'arrivée de cet
    ordre et la réponse, un `trigger` a parfaitement le droit d'avoir eu lieu.
    Un appariement implicite (« le prochain flux est l'écoute ») se tromperait
    une fois sur mille, et ce serait indiagnosticable.
    """
    return {
        "type": T_LISTEN_REQUEST,
        "listen_id": listen_id,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": 1,
        "format": "s16le",
        "chunk_ms": chunk_ms,
        "max_chunks": max_chunks,
        "server_time_ms": now_ms(),
    }


def listen_stop(*, listen_id: str, reason: str) -> dict[str, Any]:
    """Le serveur clôt l'écoute de son propre chef (durée, annulation, arrêt)."""
    return {
        "type": T_LISTEN_STOP,
        "listen_id": listen_id,
        "reason": reason,
        "server_time_ms": now_ms(),
    }


def listen_started(
    *,
    listen_id: str,
    duration_ms: int,
    chunk_ms: int,
    remaining_ms: int,
    sample_rate: int,
    joined: bool,
) -> dict[str, Any]:
    """Acquittement IMMÉDIAT vers l'opérateur, avant le moindre échantillon.

    `duration_ms` est la durée RÉELLEMENT retenue : si la demande sortait des
    bornes, on l'annonce corrigée plutôt que d'échouer — le compte à rebours
    affiché doit être juste, sinon l'opérateur croit à une panne au moment où
    le flux s'arrête à l'heure prévue par le serveur.

    `joined` distingue « j'ouvre une écoute » de « je me branche sur celle qui
    tourne déjà » : cinq lignes, et un onglet oublié ne verrouille plus
    l'écoute pendant une minute.
    """
    return {
        "type": T_LISTEN_STARTED,
        "listen_id": listen_id,
        "duration_ms": duration_ms,
        "chunk_ms": chunk_ms,
        "remaining_ms": remaining_ms,
        "sample_rate": sample_rate,
        "channels": 1,
        "format": "s16le",
        "joined": joined,
        "server_time_ms": now_ms(),
    }


def listen_progress(
    *,
    received_ms: int,
    windows: int,
    max_noisy_score: float | None,
    dropped_chunks: int,
    dropped_windows: int,
) -> dict[str, Any]:
    """Preuve de vie de l'écoute, toutes les quelques secondes.

    Les deux compteurs de perte sont le seul endroit où une surcharge devient
    visible. Sans eux, des morceaux jetés passeraient pour du silence dans la
    pièce — c'est-à-dire pour la conclusion inverse de la vérité.
    """
    return {
        "type": T_LISTEN_PROGRESS,
        "received_ms": received_ms,
        "windows": windows,
        "max_noisy_score": None if max_noisy_score is None else round(max_noisy_score, 6),
        "dropped_chunks": dropped_chunks,
        "dropped_windows": dropped_windows,
        "server_time_ms": now_ms(),
    }


def listen_ended(
    *,
    listen_id: str,
    reason: str,
    wav_name: str | None,
    duration_ms: int,
    windows: int,
    dropped_chunks: int,
    dropped_windows: int,
    partial: bool,
    analysis: dict[str, Any] | None,
    event_id: int | None,
    mp3_url: str | None,
) -> dict[str, Any]:
    """Verdict terminal, TOUJOURS envoyé — quel que soit le chemin de sortie.

    `analysis` porte le jugement du classifieur (score principal, classes du
    dessus, densité) ; il est absent quand aucune fenêtre n'a pu être classée.

    `wav_name` peut être `None` : une écoute qui n'a reçu aucun échantillon ne
    produit aucun fichier, et il faut le dire plutôt que d'annoncer un nom qui
    n'existe pas.

    `dropped_chunks` est le verdict de QUALITÉ de l'écoute : des morceaux jetés
    faute de place chez un auditeur lent. Sans lui ici, un opérateur dont la
    connexion a hoqueté ne saurait pas que ce qu'il a entendu était troué — il
    conclurait que la pièce était calme par moments.
    """
    return {
        "type": T_LISTEN_ENDED,
        "listen_id": listen_id,
        "reason": reason,
        "wav_name": wav_name,
        "duration_ms": duration_ms,
        "windows": windows,
        "dropped_chunks": dropped_chunks,
        "dropped_windows": dropped_windows,
        "partial": partial,
        "analysis": analysis,
        "event_id": event_id,
        "mp3_url": mp3_url,
        "server_time_ms": now_ms(),
    }
