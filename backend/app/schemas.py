"""SchÃ©mas pydantic â€” messages WebSocket entrants et rÃ©ponses REST.

Convention de temps, volontaire et unique dans tout le service :

  â€¢ l'API renvoie des INSTANTS en UTC, sÃ©rialisÃ©s ISO-8601 avec offset
    (Â« 2026-09-17T12:23:01+00:00 Â») â€” donc non ambigus, exploitables tels quels
    par `new Date(...)` dans le navigateur ;
  â€¢ le paramÃ¨tre `tz` ne sert qu'aux REGROUPEMENTS (histogramme, tendance,
    heatmap), que PostgreSQL calcule explicitement avec `AT TIME ZONE $tz` ;
  â€¢ il est rÃ©-Ã©chos dans la rÃ©ponse pour que le client sache comment Ã©tiqueter
    ces regroupements, sans avoir Ã  le deviner.

Ne jamais renvoyer un datetime naÃ¯f : Â« 2026-09-17T14:23:01 Â» serait interprÃ©tÃ©
comme heure locale par le navigateur, ce qui donne un rÃ©sultat juste tant que
tout le monde partage le mÃªme fuseau â€” et faux le jour oÃ¹ ce n'est plus le cas.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# WebSocket â€” client â†’ serveur
# --------------------------------------------------------------------------


class ClientHello(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["hello"]
    protocol_version: int
    client_id: str = Field(min_length=1, max_length=128)
    app_version: str | None = None
    user_agent: str | None = None
    device_sample_rate: int | None = None


class StreamStart(BaseModel):
    """MÃ©tadonnÃ©es ouvrant un Ã‰PISODE, avant les morceaux binaires.

    Comme pour `SegmentStart`, les bornes numÃ©riques ne sont pas validÃ©es ici :
    chaque refus a son code dÃ©diÃ© dans le protocole, produit par
    `ws/session.py` avec le bon message. Une validation pydantic trop stricte
    les Ã©craserait tous en un `bad_json` indiffÃ©renciÃ©.

    `pre_roll_samples` est une DURÃ‰E, pas un instant : c'est ce qui permet au
    serveur de dater le premier Ã©chantillon sur SA propre horloge, sans jamais
    faire confiance Ã  celle du vieux PC (G6).
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["stream_start"]
    seq: int
    captured_at_ms: int | None = None
    sample_rate: int
    channels: int
    format: str
    pre_roll_samples: int = 0
    chunk_samples: int = 0
    rms: float | None = None
    background_rms: float | None = None
    trigger_ratio: float | None = None


class StreamEnd(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["stream_end"]
    seq: int
    chunks: int = 0
    num_samples: int = 0
    stopped_reason: str | None = None


class SegmentStart(BaseModel):
    """MÃ©tadonnÃ©es prÃ©cÃ©dant immÃ©diatement la trame binaire PCM.

    Les bornes numÃ©riques ne sont PAS validÃ©es ici : chaque refus a un code
    d'erreur dÃ©diÃ© dans le protocole (`bad_sample_rate`, `bad_length`,
    `payload_too_large`â€¦) et c'est `ws/session.py` qui les produit, avec le
    bon message. Une validation pydantic trop stricte les Ã©craserait tous en
    un `bad_json` indiffÃ©renciÃ©.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["segment_start"]
    seq: int
    captured_at_ms: int
    sample_rate: int
    channels: int
    format: str
    num_samples: int
    rms: float | None = None
    background_rms: float | None = None
    trigger_ratio: float | None = None
    post_roll_ms: int = 1000


class ClientPing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["ping"]
    t: int | None = None


class ListenStart(StreamStart):
    """MÃ©tadonnÃ©es ouvrant une Ã‰COUTE Ã€ LA DEMANDE.

    HÃ©rite de `StreamStart` plutÃ´t que de le rÃ©utiliser avec un champ : les
    deux flux ont la même géométrie (16 kHz mono s16le, des morceaux binaires
    derrière) mais pas la même fin. Un épisode est jugé, rogné, et son
    temporaire est JETÉ s'il n'y a pas d'événement cible ; une écoute est PUBLIÉE en WAV
    quoi qu'il arrive, et peut en plus produire un épisode. Deux finalisations
    diffÃ©rentes, donc deux Ã©tats diffÃ©rents, donc deux types.

    Le bÃ©nÃ©fice secondaire est que l'exclusivitÃ© devient structurelle : le
    serveur tient `self._stream` et `self._listen` comme deux emplacements
    distincts, au lieu d'un drapeau Ã  ne pas oublier de tester.

    `listen_id` est la valeur reÃ§ue dans `listen_request`, rÃ©Ã©mise telle quelle.
    """

    type: Literal["listen_start"]
    listen_id: str = Field(min_length=1, max_length=64)
    # Motif d'un REFUS. Le poste rÃ©pond toujours â€” mÃªme quand il ne peut pas
    # ouvrir l'Ã©coute â€” pour que l'opÃ©rateur lise Â« worklet pas Ã  jour Â» ou
    # Â« page arrÃªtÃ©e Â» au lieu d'attendre cinq secondes puis de chercher une
    # panne rÃ©seau qui n'existe pas.
    refus: str | None = None


class ListenEnd(StreamEnd):
    """Fin d'Ã©coute, annoncÃ©e par le poste."""

    type: Literal["listen_end"]
    listen_id: str = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------
# WebSocket â€” canal opÃ©rateur (/ws/listen)
# --------------------------------------------------------------------------
# Protocole distinct de celui du poste : l'opÃ©rateur ne capture pas, il Ã©coute.
# Voir `ws/protocol.py` pour pourquoi les deux vocabulaires sont sÃ©parÃ©s.


class ListenBegin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["listen_begin"]
    duration_ms: int | None = None
    client_id: str | None = None


class ListenCancel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["listen_cancel"]


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------


class ClassifierInfo(BaseModel):
    backend: str
    model: str
    # Le score seuillÃ© est le MAX sur le groupe canin, pas sur une seule
    # classe. On annonce le groupe entier pour qu'un client puisse expliquer
    # ce qui a dÃ©clenchÃ©, plutÃ´t qu'un index opaque.
    dog_index: int | None
    dog_classes: list[list] = []
    bark_index: int | None = None
    threshold: float
    window_samples: int
    hop_samples: int


class HealthOut(BaseModel):
    """Liveness. Ne touche JAMAIS au classifieur, qui est derriÃ¨re un lock et
    peut Ãªtre en plein calcul.

    `degraded` est renvoyÃ© en HTTP 200, pas en 503 : un 503 ferait redÃ©marrer
    le conteneur en boucle sur un hoquet transitoire de la base, alors que
    c'est au dashboard d'afficher la dÃ©gradation.
    """

    status: Literal["ok", "degraded"]
    version: str
    # Le service tourne en deux rÃ´les sur deux ports : sans ce champ, un
    # `classifier_ready: false` sur l'admin se lirait comme une panne alors que
    # c'est le fonctionnement normal â€” le dashboard n'a pas de modÃ¨le.
    role: Literal["all", "capture", "admin"]
    uptime_s: float
    database: Literal["ok", "error"]
    media_dir: Literal["ok", "error"]
    media_writable: bool
    classifier_ready: bool
    dog_index: int | None = None
    bark_index: int | None = None
    threshold: float
    disk_free_bytes: int | None = None
    detail: str | None = None


class EventOut(BaseModel):
    id: int
    detected_at: datetime
    received_at: datetime
    client_captured_at: datetime | None
    client_id: str | None
    client_seq: int | None
    # dog_score est le critÃ¨re d'acceptation (max du groupe canin) ; bark_score
    # n'est qu'un diagnostic conservÃ© pour comparer les deux critÃ¨res.
    dog_score: float
    bark_score: float | None
    mean_dog_score: float | None
    duration_ms: int
    sample_rate: int
    mp3_url: str
    mp3_bytes: int | None
    backend: str
    model_version: str | None
    top_classes: list[Any] | None
    # Nombre de RAFALES distinctes, pas de fenêtres : c'est le chiffre que les
    # graphiques somment, et celui qu'on veut lire dans « les capturés ».
    bark_count: int = 1
    wav_name: str | None = None
    qc_score: float | None = None
    qc_valid: bool | None = None
    is_reference: bool = False


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int
    tz: str


class EventsSinceOut(BaseModel):
    """Ce qui est arrivÃ© depuis un id donnÃ© â€” pour l'actualisation en direct.

    `items` ne porte que les NOUVEAUTÃ‰S, jamais l'historique : le dashboard
    interroge souvent, et lui renvoyer la pÃ©riode entiÃ¨re Ã  chaque coup serait
    un gaspillage qui finirait par se voir passer dans le rÃ©seau.
    """

    items: list[EventOut]
    # Dernier id EFFECTIVEMENT livrÃ©. S'il y a plus de nouveautÃ©s que `limit`
    # entre deux interrogations, la suite vient au tour suivant : on avance de
    # ce qu'on a livrÃ©, jamais du maximum de la table â€” sinon on sauterait des
    # Ã©vÃ©nements sans que personne ne s'en aperÃ§oive.
    last_id: int
    count: int


class SequenceEvent(BaseModel):
    """Un clip dans une rafale, avec sa place dans l'Ã©coute."""

    id: int
    detected_at: datetime
    dog_score: float
    duration_ms: int
    mp3_url: str
    mp3_bytes: int | None = None
    rang: int  # position dans la rafale entiÃ¨re, 1-based
    offset_ms: int  # dÃ©but de ce clip dans la sÃ©quence assemblÃ©e


class EventSequenceOut(BaseModel):
    """La rafale d'événements qui contient un événement, dans l'ordre d'écoute.

    Les clips se touchent : quand le signal sonore se répète sans discontinuer, le client
    déclenche toutes les 3 s et les segments de 3 s se succèdent à quelques
    millisecondes près. Les remettre bout à bout restitue l'enregistrement
    continu — c'est le but de cette route.

    `total` est la taille de la rafale ENTIÃˆRE, `count` le nombre de clips
    renvoyÃ©s : sans les deux, une rÃ©ponse tronquÃ©e serait indiscernable d'une
    rafale courte.
    """

    anchor_id: int
    anchor_rang: int
    anchor_offset_ms: int  # oÃ¹ dÃ©marrer la lecture pour tomber sur le clic
    client_id: str | None
    gap_ms: int  # Ã©cart ayant servi Ã  chaÃ®ner
    started_at: datetime
    ended_at: datetime
    duration_ms: int  # somme des durÃ©es des clips RENVOYÃ‰S
    total: int
    count: int
    truncated: bool
    events: list[SequenceEvent]


class DeletedOut(BaseModel):
    id: int
    deleted: bool
    mp3_removed: bool


class SummaryOut(BaseModel):
    date: date_type
    tz: str
    count: int
    hours_elapsed: float
    # barks_today / heures_Ã©coulÃ©es, PAS /24 : diviser par 24 Ã  9 h fait
    # paraÃ®tre chaque matin calme et chaque soir alarmant.
    per_hour: float
    max_dog_score: float | None
    mean_dog_score: float | None
    first_detected_at: datetime | None
    last_detected_at: datetime | None
    count_prev_day: int
    count_prev_week_same_day: int


class HourBucket(BaseModel):
    hour: int
    count: int


class HistogramOut(BaseModel):
    tz: str
    buckets: list[HourBucket]
    peak_hour: int | None
    total: int


class DailyPoint(BaseModel):
    date: date_type
    count: int


class DailyOut(BaseModel):
    tz: str
    points: list[DailyPoint]
    total: int


class HeatCell(BaseModel):
    dow: int  # 1 = lundi â€¦ 7 = dimanche (convention ISO, comme PostgreSQL)
    hour: int
    count: int


class HeatmapOut(BaseModel):
    tz: str
    cells: list[HeatCell]
    max_count: int
    total: int


class TimelinePoint(BaseModel):
    id: int
    t: int  # epoch millisecondes, UTC
    score: float


class TimelineOut(BaseModel):
    tz: str
    points: list[TimelinePoint]
    total: int
    # Vrai quand la plage dÃ©passe max_points : on renvoie alors les N PLUS
    # FORTS, pas les N premiers. Renvoyer les N premiers chronologiquement
    # n'afficherait que janvier et laisserait croire que le reste est vide.
    truncated: bool


# --------------------------------------------------------------------------
# REST â€” samples d'Ã©coute Ã  la demande
# --------------------------------------------------------------------------


class OndemandEntry(BaseModel):
    """Une entrÃ©e de l'index d'analyse.

    `None` quand le sample n'a JAMAIS Ã©tÃ© soumis Ã  YAMNet : c'est ce qui porte
    le drapeau Â« dÃ©jÃ  soumis Â» du panneau, sans champ sÃ©parÃ© Ã  dÃ©synchroniser.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    bytes: int | None = None
    threshold: float | None = None
    dog_score: float | None = None
    mean_dog_score: float | None = None
    windows: int | None = None
    windows_retenues: int | None = None
    duration_ms: int | None = None
    event_id: int | None = None
    mp3_url: str | None = None
    model: str | None = None
    # Niveau du sample. Un pic trÃ¨s bas explique qu'on n'entende rien Ã  la
    # rÃ©Ã©coute â€” sans ce chiffre, un sample muet ressemble Ã  un lecteur cassÃ©.
    peak: float | None = None
    peak_dbfs: float | None = None
    analysed_at: str | None = None
    timeline_source: str | None = None
    qc_score: float | None = None
    qc_valid: bool | None = None


class OndemandSample(BaseModel):
    name: str
    bytes: int
    mtime: int
    # DurÃ©e lue dans les 44 premiers octets du WAV, sans le dÃ©coder : un
    # sample non analysÃ© doit quand mÃªme afficher sa durÃ©e.
    duration_ms: int | None = None
    analysis: OndemandEntry | None = None
    # Vrai si l'entrÃ©e a Ã©tÃ© produite avec un autre seuil ou un autre modÃ¨le que
    # ceux en service. Un seuil provisoire impose de dire avec quel point de
    # fonctionnement chaque mesure a Ã©tÃ© prise.
    stale: bool = False


class OndemandList(BaseModel):
    dir: str
    samples: list[OndemandSample]
    total_bytes: int
    disk_free_bytes: int | None = None
    threshold: float
