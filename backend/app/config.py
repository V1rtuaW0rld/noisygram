"""Configuration — pydantic-settings, lue depuis l'environnement du conteneur.

Toutes les valeurs ont un défaut utilisable : le conteneur doit démarrer même
avec un environnement vide. Les variables réellement structurantes
(DATABASE_URL, BARK_THRESHOLD, APP_TZ) sont injectées par docker-compose.yml.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Rôle du processus ---
    # Le service tourne en DEUX processus, sur deux ports :
    #
    #   capture (4466) — la page de capture et le WebSocket. C'est le seul rôle
    #                    qui charge YAMNet, et le seul qui a besoin de HTTPS
    #                    (le micro exige une origine sécurisée).
    #   admin   (4467) — le dashboard et l'API REST. N'embarque NI LiteRT, NI
    #                    l'Interpreter non thread-safe, NI leurs ~95 Mo.
    #
    # Les deux partagent la base et le volume des médias. Le découpage ne
    # rouvre PAS la question du CORS (§5.6) : chaque port sert l'API et les MP3
    # dont SA page a besoin, donc aucune requête n'est cross-origin.
    #
    # `all` reste le défaut : il monte tout sur un seul port, ce qui est
    # pratique pour le développement, pour le selftest, et pour qui ne veut
    # pas de découpage.
    app_role: str = "all"

    # --- Identité, annoncée dans hello_ack ---
    server_version: str = "0.1.0"
    protocol_version: int = 1

    # --- Persistance ---
    database_url: str = "postgresql://aboigramme:aboigramme_dev_pw@db:5432/aboigramme"
    media_dir: Path = Path("/data/media")
    static_dir: Path = Path("/app/static")
    # Procédures d'exploitation, montées depuis l'hôte (lecture seule). Le
    # montage est conditionnel : si le dossier est absent, l'application
    # démarre quand même, sans la route /docs.
    docs_dir: Path = Path("/app/docs")

    # --- Dump de diagnostic ---
    # Copie sur disque de l'audio EXACTEMENT tel qu'il part au classifieur, pour
    # pouvoir l'écouter quand les scores ne suffisent pas à comprendre. Monté
    # depuis l'hôte (`./export`), donc lisible sans `docker cp`.
    #
    # À laisser désactivé en exploitation : c'est un outil d'enquête, et il
    # écrit 96 Ko par segment classé.
    debug_dump: bool = False
    debug_dump_dir: Path = Path("/data/debug")
    # Nombre de dumps conservés. **0 = on ne supprime JAMAIS rien** — c'est la
    # valeur voulue pendant une enquête : on veut pouvoir réécouter un épisode
    # d'il y a une heure. En exploitation, mettre une borne : un épisode de
    # trois minutes pèse ~5,8 Mo.
    debug_dump_keep: int = 0

    # --- Écoute à la demande ---
    # Samples produits par le bouton « écouter » : le poste streame 60 s, on
    # l'écoute en direct, et l'audio s'écrit ici au fil de l'eau.
    #
    # Même dossier que le dump de diagnostic — c'est le MÊME bind mount
    # (`./export`), et l'opérateur n'a ainsi qu'un seul dossier à ouvrir. Le
    # suffixe, lui, est distinct (`_ondemand` contre `_debug`) : les deux
    # élagages ne peuvent donc pas se marcher dessus.
    ondemand_enabled: bool = True
    ondemand_dir: Path = Path("/data/debug")
    # Nombre de samples conservés. **0 = on ne supprime JAMAIS rien**, comme
    # `debug_dump_keep`. À laisser à 0 tant qu'on constitue le corpus de
    # calibration — c'est précisément ce corpus qui manquait. Chaque écoute de
    # 60 s pèse 1,9 Mo, soit ~14 Go/an à vingt écoutes par jour.
    ondemand_keep: int = 0

    # --- Analyse à la demande (bouton « Analyser » du panneau d'écoute) ---
    # D'où vient la timeline des sons entendus.
    #
    # `local` la calcule dans le conteneur, avec le YAMNet embarqué : mesuré à
    # 8,9 ms par seconde d'audio, soit ~0,5 s pour une minute. Rien à
    # transférer, et aucune machine de plus à garder allumée.
    #
    # `remote` délègue à un service HTTP — typiquement un YAMNet sur GPU, plus
    # rapide sur de gros volumes. Même idiome que `CLASSIFIER_BACKEND`, et
    # `classifier/remote_http.py` existe déjà pour ce cas de figure.
    analyze_backend: str = "local"
    analyze_remote_url: str | None = None
    analyze_remote_timeout_s: float = 30.0
    # Seuil d'AFFICHAGE de la timeline : en dessous, une fenêtre n'est pas
    # listée. Sans lui, une minute de vent produit cent étiquettes et la
    # timeline devient illisible. 0,3 est le seuil de l'API de l'utilisateur,
    # gardé pour que les deux sorties se ressemblent.
    #
    # À ne pas confondre avec `dog_threshold`, qui DÉCIDE : celui-ci ne fait que
    # choisir ce qu'on montre.
    analyze_timeline_min_score: float = 0.3
    # Adresse du processus `capture` sur le réseau compose. L'admin s'en sert
    # pour lui relayer ce qu'il ne peut pas faire seul : analyser un sample (il
    # n'a pas le modèle) et servir le direct (le poste est connecté là-bas).
    # Vide hors compose, où les rôles tournent dans le même processus.
    capture_url: str = "http://capture:8000"
    # Microservice QC (Quality Control acoustique)
    qc_url: str = "http://qc:8001"
    qc_enabled: bool = True
    qc_timeout_s: float = 10.0

    # --- Classifieur ---
    classifier_backend: str = "yamnet_litert"
    model_path: Path = Path("/app/models/yamnet.tflite")
    class_map_path: Path = Path("/app/models/yamnet_class_map.csv")

    # Seuil du score principal, qui est le MAX sur le GROUPE CANIN et non sur
    # la seule classe « Bark ». Mesuré sur les enregistrements de référence du
    # terrain (samples/reference/) : la classe Bark plonge à 0,262 sur de vrais
    # aboiements que Dog score à 0,586 — c'est le moins bon discriminateur du
    # groupe, et la seule qui rate des aboiements réels.
    #
    # 0,35 reste PROVISOIRE : il sépare largement les positifs mesurés
    # (≥ 0,586) des négatifs synthétiques (≤ 0,020), mais aucun fond sonore
    # réel du terrain n'a encore été mesuré. Voir README, section calibration.
    dog_threshold: float = 0.35

    # Désactivé par défaut. ATTENTION : ce flag déplace le point de
    # fonctionnement du modèle, donc l'activer (ou le désactiver après coup)
    # INVALIDE un seuil déjà réglé empiriquement. Plafonné à +12 dB et
    # conditionné à un pic < 0,1 dans le classifieur.
    peak_normalize: bool = False

    # Pont GPU distant : implémenté mais non sélectionné par défaut.
    remote_classifier_url: str | None = None
    remote_classifier_timeout_s: float = 5.0

    # --- Limites de protocole, annoncées dans hello_ack ---
    # max_segment_bytes sert de plafond de sécurité, PAS de cible : un segment
    # de 3 s @ 48 kHz pèse 288 000 octets, soit 3,6× de marge. La limite
    # ws_max_size d'uvicorn (16 777 216 par défaut) n'est pas relevée.
    # --- Épisodes streamés ---
    # Le client streame tant que ça aboie et clôt après `stream_silence_ms` de
    # calme ; le serveur classe à la volée et ne garde le fichier que si une
    # fenêtre a dépassé le seuil.
    #
    # `max_stream_ms` est une borne de DURÉE : la dépasser fait finaliser et
    # GARDER ce qu'on a — l'audio est légitime. Les bornes de sécurité
    # ci-dessous, elles, font JETER : au-delà, on ne fait plus confiance au
    # reste.
    max_stream_ms: int = 180_000
    # ⚠️ CES DEUX BORNES DOIVENT LAISSER PASSER `max_stream_ms`, SINON ELLES
    # TUENT DES ÉPISODES LÉGITIMES. 180 s à 16 kHz s16le mono font 5 760 000
    # octets ; à 4 194 304 (l'ancienne valeur), la borne d'octets coupait à
    # 131 s — plus bas que la borne de durée, donc c'est ELLE qui décidait, et
    # elle JETAIT. 132 s d'aboiements sont partis à la poubelle comme ça.
    #
    # 8 Mio laisse 45 % de marge au-dessus des trois minutes.
    max_stream_bytes: int = 8_388_608
    # Un morceau par seconde : 256 morceaux font 256 s, au-dessus des 180 s de
    # la borne de durée. Cette borne-ci ne mord donc jamais en premier — elle
    # n'est qu'un filet contre un client qui enverrait des morceaux minuscules.
    max_stream_chunks: int = 256
    # Un morceau plus gros que ça est anormal : le client en envoie d'une
    # seconde, soit 32 000 octets.
    stream_chunk_max_bytes: int = 192_000
    # Aucune trame depuis ce délai → on finalise. C'est ce qui rattrape un
    # redémarrage audio côté client, qui tue le worklet sans rien dire.
    stream_idle_ms: int = 10_000
    stream_silence_ms: int = 30_000
    stream_min_interval_ms: int = 1_000
    # En dessous de cet espace libre, un épisode est refusé AVANT d'écrire un
    # octet : le temporaire s'écrit pour tous les déclenchements, gardés ou non.
    min_free_bytes: int = 536_870_912
    # Un épisode refusé n'est conservé que sur ses premières secondes : c'est
    # là qu'est le fond sonore utile au réglage du seuil, et garder trois
    # minutes par refus remplirait le disque, `rejected/` n'ayant aucune
    # rétention.
    save_rejected_max_ms: int = 10_000

    # --- Écoute à la demande (chemin `listen_*`) ---
    # Le poste ouvre un flux continu à la demande de l'opérateur, qui l'écoute
    # en direct pendant que le serveur l'écrit et le classe.
    #
    # CES BORNES SONT DISTINCTES DE CELLES DE L'ÉPISODE, et ce n'est pas un
    # doublon : `max_stream_chunks = 256` ferait abandonner une écoute de 60 s
    # au bout de 51 s à 200 ms par morceau (300 morceaux), avec pour seul
    # symptôme une écoute qui s'arrête trop tôt. Les bornes utiles se DÉDUISENT
    # donc de la durée, et elles vivent dans les propriétés plus bas.
    listen_duration_ms: int = 60_000
    # 200 ms et non 1 s comme le chemin épisode : c'est la taille de morceau qui
    # fixe le plancher de latence de l'écoute directe. À 1 s, l'opérateur
    # entendrait avec plus d'une seconde de retard — hors sujet pour vérifier
    # qu'un poste est aux aguets.
    listen_chunk_ms: int = 200
    # Aucune trame depuis ce délai → le poste est mort ou la page est fermée.
    # Court, à la différence de `stream_idle_ms` : ici quelqu'un attend devant
    # son écran, et le WAV doit être publié tout de suite.
    listen_idle_ms: int = 5_000
    # Marge au-delà de la durée demandée avant de clore d'office. Filet pour un
    # client qui n'enverrait jamais son `listen_end`.
    listen_grace_ms: int = 5_000
    # Morceaux en attente par auditeur. Au-delà, on jette le PLUS ANCIEN : on
    # borne la latence plutôt que de la laisser dériver. Quatre morceaux de
    # 200 ms, soit 800 ms de marge avant de perdre de l'audio.
    listen_queue_chunks: int = 4

    max_segment_bytes: int = 1_048_576
    min_sample_rate: int = 8_000
    max_sample_rate: int = 96_000
    max_segment_ms: int = 10_000
    max_pending: int = 4

    # --- Réglages poussés au client via config_patch ---
    # Permet de régler les seuils du client sans redéploiement : utile pour une
    # boîte qu'il faut aller visiter physiquement pour changer un curseur.
    client_cooldown_ms: int = 3_000
    client_trigger_ratio: float = 2.5
    client_min_rms_floor: float = 0.004
    # Garde-tempête du client : au-delà de `client_storm_max` déclenchements
    # dans `client_storm_window_ms`, la capture est suspendue
    # `client_storm_suspend_ms`. Ce n'est PAS qu'une protection : c'est ce qui
    # décide de la longueur maximale d'une rafale enregistrée d'un seul tenant.
    # À 12/min, la capture se coupait au bout de 36 s — un chien qui aboie deux
    # minutes n'était jamais enregistré continu, et aucun recollage ne pouvait
    # le reconstituer. 40 laisse environ deux minutes.
    client_storm_max: int = 40
    client_storm_window_ms: int = 60_000
    client_storm_suspend_ms: int = 60_000
    # Longueur d'un épisode, réglable sans redéployer le poste : c'est elle qui
    # décide de la place occupée sur le disque.
    client_stream_enabled: bool = True
    client_stream_silence_ms: int = 30_000
    client_stream_max_ms: int = 180_000
    # Autorise le bouton « écouter » à commander ce poste. Un drapeau côté
    # client et non seulement côté serveur : couper la fonctionnalité depuis le
    # serveur sans que le poste le sache laisserait la page d'écoute attendre un
    # audio qui ne viendrait jamais.
    client_listen_enabled: bool = True

    # --- Divers ---
    app_tz: str = "Europe/Paris"
    log_level: str = "INFO"
    save_rejected: bool = False
    uvicorn_workers: int = 1

    @field_validator("app_role")
    @classmethod
    def _role_connu(cls, v: str) -> str:
        role = v.strip().lower()
        if role not in ("all", "capture", "admin"):
            # Refusé au démarrage : un rôle mal orthographié monterait sinon
            # un service qui répond 404 partout, ce qui ressemble à un bug de
            # routage et non à une faute de frappe.
            raise ValueError(f"APP_ROLE inconnu : {v!r} (attendu : all, capture, admin)")
        return role

    @model_validator(mode="after")
    def _bornes_de_flux_coherentes(self) -> "Settings":
        """La borne de VOLUME doit laisser passer la borne de DURÉE.

        Sinon c'est elle qui décide, à la place de la durée, et un épisode
        légitime est coupé plus tôt que prévu. Avec l'ancien réglage
        (4 MiB contre 180 s), la coupure tombait à 131 s — et un chien qui
        aboyait deux minutes voyait son enregistrement tronqué.

        Refusé AU DÉMARRAGE, comme APP_ROLE : une incohérence entre deux
        réglages ne se voit qu'au moment où elle mord, c'est-à-dire au pire
        moment, sur le terrain, et elle se lit comme une panne du client.
        """
        besoin = self.max_stream_ms // 1000 * 16_000 * 2
        if self.max_stream_bytes < besoin:
            raise ValueError(
                f"MAX_STREAM_BYTES ({self.max_stream_bytes}) ne couvre pas "
                f"MAX_STREAM_MS ({self.max_stream_ms} ms → {besoin} octets à "
                "16 kHz s16le). La borne de volume couperait avant celle de "
                "durée, et déciderait à sa place."
            )
        return self

    @field_validator("analyze_backend")
    @classmethod
    def _analyse_connue(cls, v: str) -> str:
        # Refusé au démarrage, comme APP_ROLE : un `analyze_backend` mal
        # orthographié ne se verrait qu'au premier clic sur « Analyser », et se
        # lirait comme une panne du panneau plutôt que comme une faute de frappe.
        connu = {"local", "remote"}
        if v.strip().lower() not in connu:
            raise ValueError(f"ANALYZE_BACKEND inconnu : {v!r} (attendu : local, remote)")
        return v.strip().lower()

    @field_validator("app_tz")
    @classmethod
    def _tz_connue(cls, v: str) -> str:
        # Validé au démarrage plutôt qu'à la première requête de stats : une
        # faute de frappe dans .env doit tuer le conteneur tout de suite, avec
        # un message clair, pas renvoyer un 500 trois heures plus tard.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"APP_TZ inconnu : {v!r}") from exc
        return v

    @property
    def sert_capture(self) -> bool:
        return self.app_role in ("all", "capture")

    @property
    def sert_admin(self) -> bool:
        return self.app_role in ("all", "admin")

    @property
    def charge_classifieur(self) -> bool:
        # Seul le rôle capture qualifie de l'audio. Le dashboard lit la base.
        return self.app_role in ("all", "capture")

    # --- Bornes dérivées du chemin d'écoute ---
    # Dérivées de la DURÉE, jamais reprises du chemin épisode : voir le
    # commentaire des réglages `listen_*`. La marge couvre le pré-roll éventuel,
    # les morceaux irréguliers et un `listen_end` en retard.

    @property
    def listen_chunk_bytes(self) -> int:
        """Octets d'un morceau de `listen_chunk_ms`, en s16le mono 16 kHz."""
        return int(self.listen_chunk_ms * 16000 / 1000) * 2

    @property
    def listen_max_ms(self) -> int:
        """Au-delà, le serveur clôt de son propre chef."""
        return self.listen_duration_ms + self.listen_grace_ms

    @property
    def listen_max_chunks(self) -> int:
        """Nombre de morceaux toléré, avec 50 % de marge sur la durée demandée."""
        par_ms = max(1, self.listen_chunk_ms)
        return int(self.listen_max_ms / par_ms * 1.5) + 32

    @property
    def listen_max_bytes(self) -> int:
        """Octets tolérés : la durée maximale à 16 kHz s16le mono, plus 50 %."""
        return int(self.listen_max_ms / 1000 * 16000 * 2 * 1.5)

    @field_validator("dog_threshold")
    @classmethod
    def _seuil_dans_les_bornes(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"DOG_THRESHOLD doit être dans [0,1], reçu {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
