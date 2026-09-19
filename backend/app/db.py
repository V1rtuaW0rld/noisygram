"""Accès PostgreSQL — asyncpg brut, pas de SQLAlchemy (§5.5).

Cinq requêtes dans toute l'application : il n'y a aucune valeur d'ORM à en
tirer, et on économise une dépendance, un greenlet et une couche.

Le pool est créé dans le `lifespan` de FastAPI, JAMAIS à l'import : asyncpg
lie ses connexions à la boucle d'événements courante, et un pool créé à
l'import casse silencieusement le reload (G14).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

# DDL idempotent, exécuté au démarrage. EN UNE SEULE instruction.
#
# ⚠️ asyncpg accepte plusieurs instructions séparées par « ; » dans un
# execute(), MAIS PAS si elles contiennent des paramètres $1 : il bascule alors
# sur le protocole étendu et rejette le multi-instruction (G17). Ce DDL ne
# contient donc aucun paramètre ; le fuseau n'apparaît que dans les SELECT,
# paramétrés normalement.
DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id                 BIGSERIAL   PRIMARY KEY,
    -- Instant serveur de l'événement : now() - post_roll, JAMAIS l'horloge du
    -- client (G6). client_captured_at est stocké à part, en diagnostic.
    detected_at        TIMESTAMPTZ NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_captured_at TIMESTAMPTZ,
    client_id          TEXT,
    client_seq         BIGINT,
    noisy_score        REAL        NOT NULL,
    bark_score         REAL,
    mean_noisy_score   REAL,
    duration_ms        INTEGER     NOT NULL,
    sample_rate        INTEGER     NOT NULL,
    mp3_path           TEXT        NOT NULL,
    mp3_bytes          INTEGER,
    backend            TEXT        NOT NULL DEFAULT 'yamnet-litert',
    model_version      TEXT,
    top_classes        JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Une ligne n'est plus un clip de 3 s mais un ÉPISODE, qui peut durer
    -- plusieurs minutes. La borne est un filet de sécurité, pas une vérité :
    -- l'app plafonne à `max_stream_ms` (3 min), et la marge laisse de quoi
    -- relever ce plafond sans migration.
    noisy_count     INTEGER     NOT NULL DEFAULT 1,
    -- Vrai si des fenêtres n'ont pas pu être classées (surcharge) : le score
    -- ne couvre alors qu'une partie de l'épisode, et il faut le DIRE plutôt
    -- que de laisser croire à une couverture totale.
    partial         BOOLEAN     NOT NULL DEFAULT FALSE,
    stopped_reason  TEXT,
    window_count    INTEGER,
    wav_name        TEXT,
    qc_score        REAL,
    is_reference    BOOLEAN     NOT NULL DEFAULT FALSE,
    CONSTRAINT events_noisy_score_chk CHECK (noisy_score >= 0.0 AND noisy_score <= 1.0),
    CONSTRAINT events_bark_score_chk  CHECK (bark_score IS NULL OR (bark_score >= 0.0 AND bark_score <= 1.0)),
    CONSTRAINT events_duration_chk    CHECK (duration_ms BETWEEN 100 AND 300000),
    CONSTRAINT events_rate_chk        CHECK (sample_rate BETWEEN 8000 AND 96000)
);

CREATE UNIQUE INDEX IF NOT EXISTS events_client_seq_uniq
    ON events (client_id, client_seq) WHERE client_seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS events_detected_at_desc ON events (detected_at DESC);

-- ⚠️ L'index sur le score est créé par la MIGRATION 7, pas ici. Le DDL tourne
-- AVANT les migrations (voir `lifespan`) : sur une base antérieure au
-- renommage, la colonne s'appelle encore `dog_score` et un
-- `CREATE INDEX ... (noisy_score)` échouerait, faisant tomber le démarrage.

-- Reconstitution des rafales (GET /api/events/sequence) : la fenêtre chaîne les
-- événements d'un MÊME client dans l'ordre du temps. Sans cet index, chaque
-- clic trierait tout l'historique du client.
CREATE INDEX IF NOT EXISTS events_client_time_idx  ON events (client_id, detected_at);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
"""

# Migrations incrémentales, appliquées dans l'ordre de leur numéro.
#
# ELLES SONT INDISPENSABLES, et c'est une découverte : le DDL ci-dessus est un
# `CREATE TABLE IF NOT EXISTS`. Sur une base qui existe déjà, le modifier ne
# change RIEN — la table n'est pas recréée, l'ancienne contrainte reste, et le
# premier épisode de trois minutes serait rejeté par PostgreSQL APRÈS que le
# MP3 ait été écrit, laissant un fichier orphelin.
#
# Comme le DDL, chaque bloc doit rester SANS PARAMÈTRE `$n` : asyncpg bascule
# alors sur le protocole étendu et refuse le multi-instruction (G17).
MIGRATIONS: dict[int, str] = {
    2: """
    ALTER TABLE events DROP CONSTRAINT IF EXISTS events_duration_chk;
    ALTER TABLE events ADD  CONSTRAINT events_duration_chk
        CHECK (duration_ms BETWEEN 100 AND 300000);
    ALTER TABLE events ADD COLUMN IF NOT EXISTS noisy_count     INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS partial        BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS stopped_reason TEXT;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS window_count   INTEGER;
    """,
    3: """
    ALTER TABLE events ADD COLUMN IF NOT EXISTS wav_name       TEXT;
    """,
    4: """
    ALTER TABLE events ADD COLUMN IF NOT EXISTS qc_score       REAL;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS is_reference   BOOLEAN NOT NULL DEFAULT FALSE;
    CREATE TABLE IF NOT EXISTS qc_config (
        key        TEXT PRIMARY KEY,
        value      JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    INSERT INTO qc_config (key, value) VALUES (
        'duration_thresholds',
        '{"<=0.5s": 19, "<=1s": 25, "<=2s": 33, "<=3s": 43, "<=4s": 47, "<=5s": 53, "<=6s": 60, "<=7s": 65, "<=10s": 70, ">10s": 75}'::jsonb
    ) ON CONFLICT (key) DO NOTHING;
    """,
    5: """
    ALTER TABLE events ADD COLUMN IF NOT EXISTS qc_valid       BOOLEAN;
    """,
    6: """
    CREATE TABLE IF NOT EXISTS qc_snippets (
        id               SERIAL PRIMARY KEY,
        event_id         INTEGER REFERENCES events(id) ON DELETE SET NULL,
        wav_name         TEXT NOT NULL,
        snippet_filename TEXT NOT NULL UNIQUE,
        debut_s          REAL NOT NULL,
        fin_s            REAL NOT NULL,
        duration_s       REAL NOT NULL,
        sound            TEXT,
        score            REAL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS qc_snippets_event_id_idx ON qc_snippets(event_id);
    CREATE INDEX IF NOT EXISTS qc_snippets_wav_name_idx ON qc_snippets(wav_name);
    """,
    # 7 — Le schéma quitte le vocabulaire d'origine du projet : les colonnes
    #     portaient encore `dog_*` alors qu'il qualifie du bruit générique.
    #
    #     ⚠️ Elle doit être CONDITIONNELLE, parce qu'elle s'applique à deux
    #     bases de formes différentes :
    #
    #     • base antérieure — les colonnes s'appellent `dog_score`,
    #       `mean_dog_score`, `bark_count` : on les renomme ;
    #
    #     • base NEUVE — le DDL ci-dessus les a déjà créées sous le nouveau nom,
    #       MAIS la migration 2 est repassée derrière et a rajouté un
    #       `bark_count` à côté du `noisy_count`, puisque `ADD COLUMN IF NOT
    #       EXISTS` ne regarde que le nom qu'on lui donne. Cette colonne-là est
    #       vide, sans emploi, et on la supprime.
    #
    #     Un `RENAME COLUMN` sec échouerait donc sur base neuve (« column
    #     noisy_count already exists »), et un `DROP` sec détruirait la vraie
    #     colonne sur base ancienne. D'où les tests d'existence.
    7: """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'events'
                     AND column_name = 'dog_score')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'events'
                     AND column_name = 'noisy_score') THEN
            ALTER TABLE events RENAME COLUMN dog_score TO noisy_score;
        END IF;

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'events'
                     AND column_name = 'mean_dog_score')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'events'
                     AND column_name = 'mean_noisy_score') THEN
            ALTER TABLE events RENAME COLUMN mean_dog_score TO mean_noisy_score;
        END IF;

        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'events'
                     AND column_name = 'bark_count') THEN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = 'events'
                         AND column_name = 'noisy_count') THEN
                ALTER TABLE events DROP COLUMN bark_count;
            ELSE
                ALTER TABLE events RENAME COLUMN bark_count TO noisy_count;
            END IF;
        END IF;

        -- Les noms de contrainte et d'index survivent au RENAME COLUMN : la
        -- définition suit la colonne, seule l'étiquette reste en arrière.
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'events_dog_score_chk') THEN
            ALTER TABLE events RENAME CONSTRAINT events_dog_score_chk
                TO events_noisy_score_chk;
        END IF;

        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'events_dog_score_idx') THEN
            ALTER INDEX events_dog_score_idx RENAME TO events_noisy_score_idx;
        END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS events_noisy_score_idx ON events (noisy_score DESC);
    """,
    # 8 — Le projet : ce que cette installation cherche à compter.
    #
    #     Jusqu'ici le groupe de classes YAMNet était une constante du code
    #     (`NOISY_CLASS_NAMES`), et `load()` refusait même de démarrer si le
    #     class map ne contenait pas Bark et Dog — l'hypothèse canine était une
    #     condition de démarrage, pas seulement un défaut.
    #
    #     La cible devient une donnée. UNE SEULE ligne, parce qu'une
    #     installation compte une chose à la fois et qu'un réglage à plusieurs
    #     lignes serait une fausse généralité. Le jour où il faudra plusieurs
    #     cibles nommées, ce sera une migration, pas une devinette.
    8: """
    CREATE TABLE IF NOT EXISTS projet_config (
        id         INTEGER     PRIMARY KEY DEFAULT 1,
        nom        TEXT,
        terme      TEXT,
        classes    JSONB       NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT projet_config_ligne_unique CHECK (id = 1)
    );
    """,
}


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Stockage en UTC. Tout découpage horaire se fait ensuite explicitement
    # avec « AT TIME ZONE $tz » dans les SELECT, donc la réponse ne dépend PAS
    # du TZ du conteneur — c'est tout l'intérêt.
    await conn.execute("SET TIME ZONE 'UTC'")


async def connect(dsn: str) -> asyncpg.Pool:
    """Crée le pool. À appeler depuis le lifespan, jamais à l'import."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=8,
        init=_init_connection,
        command_timeout=30,
    )
    log.info("pool PostgreSQL créé")
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("pool PostgreSQL fermé")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError(
            "pool non initialisé — connect() doit être appelé dans le lifespan"
        )
    return _pool


async def run_ddl() -> None:
    """Applique le DDL. Idempotent, sans paramètre, en une seule instruction."""
    await pool().execute(DDL)
    version = await pool().fetchval("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    log.info("schéma en place (schema_version=%s)", version)


async def migrate() -> int:
    """Applique les migrations manquantes, sous verrou consultatif.

    Le verrou n'est pas décoratif : capture et admin démarrent ENSEMBLE et
    exécutent tous deux ceci. Lire `schema_version` puis migrer serait un
    TOCTOU — les deux appliqueraient la même migration, et le second `ADD
    CONSTRAINT` échouerait sur une contrainte déjà présente. Le verrou
    transactionnel sérialise, et la relecture DANS la transaction rend la
    course inoffensive.

    Le DDL est transactionnel en PostgreSQL : une coupure au milieu ne laisse
    pas la table sans contrainte de durée.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(472108)")

            courante = int(
                await conn.fetchval(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                )
                or "1"
            )
            for version, sql in sorted(MIGRATIONS.items()):
                if version <= courante:
                    continue
                await conn.execute(sql)
                courante = version
                log.info("migration %d appliquée", version)

            # UPDATE et non INSERT … ON CONFLICT DO NOTHING : ce dernier ne
            # mettrait JAMAIS à jour une valeur existante, et la version
            # resterait figée à 1 en donnant l'illusion d'avoir migré.
            await conn.execute(
                "UPDATE schema_meta SET value = $1, updated_at = now() "
                "WHERE key = 'schema_version'",
                str(courante),
            )
    return courante


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    return await pool().fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> Optional[asyncpg.Record]:
    return await pool().fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    return await pool().fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    return await pool().execute(sql, *args)
