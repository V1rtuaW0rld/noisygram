"""Samples d'écoute à la demande — le dossier `export/` de l'hôte.

Un « ondemand » est une prise de son déclenchée par l'opérateur : le poste
streame 60 secondes, on l'écoute en direct, et l'audio s'écrit ici au fil de
l'eau. C'est le seul producteur de ce dossier qui soit délibéré — les
`*_debug.wav` voisins sont des sous-produits d'enquête de `debugdump.py`.

DEUX CHOSES QUE CE MODULE DOIT GARANTIR, et qui expliquent sa forme :

1. **Le fichier finit en WAV quoi qu'il arrive.** L'en-tête est écrit EN
   PREMIER, avec des tailles nulles, puis complété à la fin. Un processus tué
   en pleine écriture laisse donc un `.part` dont l'en-tête est faux de
   quelques octets — mais réparable, puisque `taille - 44` donne le nombre
   d'échantillons. `media.sweep_spool` SUPPRIME ses temporaires d'épisode ; ici
   on RÉCUPÈRE, parce qu'un épisode non finalisé n'a pas été jugé alors qu'une
   écoute est la seule copie d'une prise de son réelle.

2. **Le `.part` vit dans le dossier de destination.** `os.replace` n'est
   atomique que sur un même volume (`media.spool_relpath`), et `export/` est un
   bind mount distinct du volume `media` : un temporaire sous `/data/media/.tmp`
   devrait être copié, et la copie n'est pas atomique.

Ce module n'a AUCUN couplage WebSocket ni asyncio : il est testable seul, et
c'est délibéré — c'est ici que se joue la garantie n° 1.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ..audio.pcm import pcm16_to_float32
from ..audio.wav import wav_bytes

log = logging.getLogger(__name__)

# 44 octets : RIFF(12) + fmt (8 + 16) + data (8), pour du PCM 16 bits sans chunk
# supplémentaire. Vérifié, et c'est ce que produit le module `wave` de la
# stdlib — donc ce que produit `wav_bytes`. Les deux tailles à compléter sont
# l'offset 4 (RIFF, = 36 + données) et l'offset 40 (data).
HEADER_BYTES = 44

# « .wav.part » et non « .part » nu : le fichier est un WAV valide dès la
# première trame, et le nom le dit. Un humain qui ouvre le dossier comprend.
PART_SUFFIX = ".wav.part"

# Le suffixe qui distingue un ondemand d'un dump de debug. Il est INDISPENSABLE
# et pas décoratif : `debugdump._elaguer` globbe `*_debug.wav` et supprime tout
# sauf les N plus récents. Deux suffixes distincts, donc deux élagages distincts,
# et les samples de l'utilisateur ne peuvent pas être emportés par un élagage de
# debug.
LABEL = "ondemand"

INDEX_NAME = "ondemand_index.json"
INDEX_VERSION = 1


# ------------------------------------------------------------------ nommage


def sample_name(now_utc: datetime, app_tz: str, duration_s: float) -> str:
    """`20260918-143205-123_ondemand_60s.wav` — horodatage d'abord, comme les
    fichiers de debug déjà présents dans le dossier.

    Heure LOCALE et non UTC, à la différence de `debugdump.dump`. C'est le
    principe déjà écrit pour l'arborescence des MP3 (`media.py`) : c'est un
    humain qui ouvrira ce dossier, et il cherchera « l'écoute de 14 h 32 », pas
    « celle de 12 h 32 UTC ».

    Pas de `:` dans le nom : illégal sur Windows, et le poste extérieur en est
    un — un fichier qu'on ne peut pas copier sur la machine qui l'a produit
    serait une plaisanterie.
    """
    local = now_utc.astimezone(ZoneInfo(app_tz))
    return (
        f"{local.strftime('%Y%m%d-%H%M%S')}-{local.microsecond // 1000:03d}"
        f"_{LABEL}_{duration_s:.0f}s.wav"
    )


def is_sample(name: str) -> bool:
    """Un nom de sample ondemand, et rien d'autre : ni un `.part`, ni un
    `_debug.wav`, ni un chemin."""
    base = os.path.basename(name)
    return (
        base == name
        and base.endswith(".wav")
        and not base.endswith(PART_SUFFIX)
        and f"_{LABEL}_" in base
        and not base.startswith(".")
    )


def wav_duration_ms(path: Path) -> int | None:
    """Durée d'un WAV, lue dans ses 44 premiers octets. Ne décode RIEN.

    Le panneau liste aussi bien un sample de 60 s qu'un dump de debug de trois
    minutes, et afficher « — » faute d'avoir voulu charger 5 Mo en mémoire pour
    compter des échantillons serait une paresse coûteuse.

    Rend None si le fichier n'est pas du PCM 16 bits mono : on préfère ne rien
    dire que dire une durée fausse.
    """
    try:
        with open(path, "rb") as fh:
            entete = fh.read(HEADER_BYTES)
    except OSError:
        return None
    if len(entete) < HEADER_BYTES or entete[:4] != b"RIFF" or entete[36:40] != b"data":
        return None
    canaux = struct.unpack("<H", entete[22:24])[0]
    bits = struct.unpack("<H", entete[34:36])[0]
    sr = struct.unpack("<I", entete[24:28])[0]
    octets = struct.unpack("<I", entete[40:44])[0]
    if canaux != 1 or bits != 16 or not sr:
        return None
    return round(octets / 2 / sr * 1000)


def _header(sample_rate: int) -> bytes:
    """Les 44 octets d'en-tête, tailles à zéro.

    On les obtient en sérialisant un WAV VIDE plutôt qu'en les écrivant à la
    main : le module `wave` de la stdlib reste alors la seule autorité sur le
    format, et une future version qui ajouterait un chunk `LIST` ne nous
    laisserait pas produire des en-têtes de 44 octets qu'elle ne relirait plus.
    """
    return wav_bytes(np.zeros(0, dtype=np.float32), sample_rate)[:HEADER_BYTES]


# ------------------------------------------------------------------- écriture


class WavSpool:
    """WAV en cours d'écriture. Le `.part` DEVIENT le `.wav`.

    Créé paresseusement au premier octet : si le poste n'envoie rien, aucun
    fichier n'apparaît — pas même un `.part` vide qui ferait croire à une
    écoute qui a échoué.
    """

    def __init__(self, directory: Path, name: str, sample_rate: int) -> None:
        self.directory = directory
        self.name = name
        self.sample_rate = sample_rate
        self.final = directory / name
        self.part = directory / (name + ".part")
        self.samples = 0
        self._fh = None
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        """Le fichier sur disque, `.part` tant qu'on écrit, `.wav` ensuite."""
        return self._path

    @property
    def data_bytes(self) -> int:
        return self.samples * 2

    @property
    def duration_s(self) -> float:
        return self.samples / self.sample_rate if self.sample_rate else 0.0

    def write(self, data: bytes) -> None:
        """Ajoute du PCM s16le. N'échoue jamais en silence : l'appelant décide."""
        if self._fh is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.part, "w+b")
            self._path = self.part
            self._fh.write(_header(self.sample_rate))
        self._fh.write(data)
        self.samples += len(data) // 2

    def _patch_header(self) -> None:
        """Complète les deux tailles, sur le fichier OUVERT.

        `flush` sans `fsync` : on ne cherche pas à survivre à une coupure de
        courant, seulement à un `kill -9` du processus — que le cache du noyau
        absorbe déjà. Un `fsync` par écoute coûterait un aller-retour disque
        pour un bénéfice qu'on ne saurait pas mesurer.
        """
        if self._fh is None:
            return
        n = self.data_bytes
        self._fh.seek(4)
        self._fh.write(struct.pack("<I", 36 + n))
        self._fh.seek(40)
        self._fh.write(struct.pack("<I", n))
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def finalize(self) -> Path | None:
        """Complète l'en-tête et publie en `.wav`. Rend le chemin, ou None si
        rien n'a jamais été écrit.

        Appelé sur TOUTES les sorties, y compris les sorties en erreur : voir
        l'en-tête du module. Ne lève pas — un échec de publication est
        journalisé, et le `.part` reste sur disque, réparable.
        """
        if self._fh is None:
            return None
        try:
            self._patch_header()
            self.close()
            os.replace(self.part, self.final)
            self._path = self.final
            return self.final
        except OSError as exc:
            log.warning("publication du WAV impossible (%s) : %r", self.name, exc)
            self.close()
            return None

    def read_range(self, premier: int, dernier: int) -> np.ndarray:
        """Les échantillons [premier, dernier) en float32, en une seule lecture.

        Même sémantique qu'`EpisodeWriter.read_range` — des indices
        d'ÉCHANTILLONS, pas d'octets — pour que le rognage et le stockage
        soient partagés sans adaptateur.
        """
        self.close()
        chemin = self._path or self.part
        debut, fin = max(0, premier), max(0, dernier)
        if fin <= debut:
            return np.zeros(0, dtype=np.float32)
        try:
            with open(chemin, "rb") as fh:
                fh.seek(HEADER_BYTES + debut * 2)
                brut = fh.read((fin - debut) * 2)
        except OSError as exc:
            log.warning("relecture du WAV impossible (%s) : %r", chemin, exc)
            return np.zeros(0, dtype=np.float32)
        return pcm16_to_float32(brut, channels=1)


# ------------------------------------------------------------------ balayage


def _promote(part: Path) -> Path | None:
    """Complète l'en-tête d'un `.part` orphelin et le renomme en `.wav`."""
    taille = part.stat().st_size
    if taille < HEADER_BYTES:
        part.unlink()
        return None
    n = taille - HEADER_BYTES
    with open(part, "r+b") as fh:
        fh.seek(4)
        fh.write(struct.pack("<I", 36 + n))
        fh.seek(40)
        fh.write(struct.pack("<I", n))
    final = part.with_name(part.name[: -len(".part")])
    os.replace(part, final)
    return final


def sweep(directory: Path) -> int:
    """Récupère les `.part` laissés par un arrêt brutal. Rend le nombre promu.

    Appelé au démarrage : à cet instant aucune écoute ne peut être en cours
    (elles vivent dans les sessions WebSocket, qui n'existent pas encore), donc
    tout `.part` trouvé est par définition orphelin.

    On ne supprime RIEN d'autre que les fichiers sans audio. Un `.part` de plus
    de 44 octets est une vraie prise de son, et c'est la seule copie.
    """
    if not directory.is_dir():
        return 0
    n = 0
    for part in sorted(directory.glob(f"*{PART_SUFFIX}")):
        try:
            final = _promote(part)
        except OSError as exc:
            log.warning("récupération de %s impossible : %r", part.name, exc)
            continue
        if final is not None:
            ms = wav_duration_ms(final) or 0
            log.warning(
                "écoute interrompue par un arrêt brutal — WAV récupéré : %s (%.1f s)",
                final.name,
                ms / 1000,
            )
            n += 1
    return n


def prune(directory: Path, keep: int) -> int:
    """Ne garde que les `keep` samples ondemand les plus récents.

    `keep = 0` signifie « ne supprime jamais », comme `DEBUG_DUMP_KEEP`.

    Ne globbe QUE les samples ondemand : les `*_debug.wav` du dossier ont leur
    propre élagage (`debugdump._elaguer`), et les attraper ici ferait de ce
    réglage une façon détournée de détruire des dumps d'enquête.
    """
    if keep <= 0 or not directory.is_dir():
        return 0
    samples = sorted(
        (p for p in directory.glob("*.wav") if is_sample(p.name)),
        key=lambda p: p.stat().st_mtime,
    )
    n = 0
    for vieux in samples[:-keep]:
        try:
            vieux.unlink()
            n += 1
        except OSError:
            continue
    return n


# -------------------------------------------------------------------- index


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for bloc in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


class AnalysisIndex:
    """Les analyses déjà faites, dans le dossier, indexées par CONTENU.

    La clé est le sha256 du WAV et non son nom : l'utilisateur renomme et
    déplace ses fichiers à la main, et une analyse perdue au premier renommage
    serait une trahison. Conséquence assumée, à écrire noir sur blanc :

      · survit à un renommage et à un déplacement DANS le dossier ;
      · ne survit PAS à une modification du WAV (voulu : toucher l'audio
        invalide l'analyse) ;
      · ne survit PAS à une sortie du dossier (le panneau ne voit que lui).

    Un seul fichier d'index et non un sidecar par WAV : ce dossier est ouvert
    et manipulé à la main, y doubler le nombre de fichiers serait hostile — et
    un sidecar orphelin après un déplacement serait pire que pas de sidecar.

    Aucun verrou : capture est mono-processus et mono-boucle, et `put` + `save`
    ne contiennent pas le moindre `await`. Deux requêtes HTTP ne peuvent donc
    pas s'entrelacer au milieu d'une écriture.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / INDEX_NAME
        self._entries: dict[str, dict] | None = None

    def _charger(self) -> dict[str, dict]:
        if self._entries is None:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                entrees = data.get("entries")
                self._entries = entrees if isinstance(entrees, dict) else {}
            except FileNotFoundError:
                self._entries = {}
            except (OSError, ValueError) as exc:
                # Un index illisible ne doit pas emporter le panneau : on
                # repart de zéro, et on le DIT — sinon les analyses semblent
                # disparaître sans raison.
                log.warning("index ondemand illisible (%s) : %r", self.path, exc)
                self._entries = {}
        return self._entries

    def get(self, digest: str) -> dict | None:
        return self._charger().get(digest)

    def put(self, digest: str, entry: dict) -> None:
        self._charger()[digest] = entry
        self.save()

    def forget(self, digest: str) -> None:
        if self._charger().pop(digest, None) is not None:
            self.save()

    def save(self) -> None:
        """Écriture atomique : un index tronqué emporterait tout le panneau."""
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {"version": INDEX_VERSION, "entries": self._charger()}
        tmp = self.path.with_name(self.path.name + ".part")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("écriture de l'index ondemand impossible : %r", exc)


def disk_usage(directory: Path) -> tuple[int, int]:
    """(octets des samples, nombre de samples). Le poids du dossier est affiché
    par le panneau : c'est ce qui rend la croissance visible avant qu'elle ne
    devienne un problème."""
    if not directory.is_dir():
        return 0, 0
    total, n = 0, 0
    for p in directory.glob("*.wav"):
        if not is_sample(p.name):
            continue
        try:
            total += p.stat().st_size
            n += 1
        except OSError:
            continue
    return total, n
