"""Compte les unités sonores dans une séquence — les aboiements d'une rafale.

    docker compose exec capture python -m app.tools.compteur --event 541
    docker compose exec capture python -m app.tools.compteur /data/debug/x.wav

⚠️ **Le critère n'est PAS encore calibré.** On sait compter les pics d'une
enveloppe ; on ne sait pas encore dire lesquels sont des aboiements. Cet outil
existe pour ça : il affiche chaque candidat avec de quoi juger, et REFUSE de
trancher à ta place. Le compte qu'il rend est une proposition à vérifier à
l'oreille, pas un résultat.

Pourquoi ce n'est pas dans la base : une colonne remplie par un critère non
calibré donne des chiffres qui ont l'air d'autorité. Tant qu'on ne sait pas
compter juste, on ne stocke rien.

Le raisonnement physique, lui, est solide et vient de l'utilisateur : un chien
qui aboie au même endroit, à la même distance, produit des aboiements de MÊME
AMPLITUDE. Un pic franchement plus fort que les autres est donc suspect — ce
n'est pas la même source. C'est ce que fait `ecart` ci-dessous.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import numpy as np

# --- Réglages, tous discutables -------------------------------------------
# Les valeurs par défaut sont un POINT DE DÉPART, pas une vérité mesurée.
FEN_MS = 10.0  # largeur de trame de l'enveloppe
HOP_MS = 5.0  # pas : 5 ms de résolution, contre 480 ms pour le score YAMNet
FACTEUR_FOND = 4.0  # un pic doit valoir ça de fois le fond local
REFRACT_MS = 120.0  # deux aboiements ne peuvent pas être plus proches
ECART_MIN = 0.40  # niveau mini d'un pic, en multiple de la médiane des pics
ECART_MAX = 2.00  # au-delà, la source a changé : ce n'est plus le même chien


def lire_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        raw = w.readframes(n)
    # Le chemin d'analyse lit en float32 ; on reste cohérent avec lui.
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def enveloppe(x: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """RMS par trame, lissée. Le lissage évite qu'une trame tombe dans le
    creux entre deux harmoniques d'un même aboiement et le coupe en deux."""
    f, h = int(FEN_MS * sr / 1000), int(HOP_MS * sr / 1000)
    n = 1 + max(0, (len(x) - f) // h)
    e = np.array([np.sqrt(np.mean(x[i * h : i * h + f] ** 2)) for i in range(n)])
    return np.convolve(e, np.ones(3) / 3, mode="same"), h / sr


def fond_local(e: np.ndarray, dt: float, fen_s: float = 1.5) -> np.ndarray:
    """Médiane glissante : le bruit de fond bouge (le vent monte et descend),
    un fond global ferait passer une bourrasque pour une salve."""
    demi = max(1, int(fen_s / 2 / dt))
    return np.array(
        [np.median(e[max(0, i - demi) : i + demi + 1]) for i in range(len(e))]
    )


def candidats(e: np.ndarray, dt: float, fond: np.ndarray) -> list[int]:
    seuil = FACTEUR_FOND * fond
    refr = max(1, int(REFRACT_MS / 1000 / dt))
    pics = [i for i in range(1, len(e) - 1) if e[i] >= e[i - 1] and e[i] > e[i + 1] and e[i] > seuil[i]]
    gardes: list[int] = []
    for i in pics:
        if not gardes or i - gardes[-1] >= refr:
            gardes.append(i)
        elif e[i] > e[gardes[-1]]:
            gardes[-1] = i  # on garde le plus fort du groupe
    return gardes


def largeur_ms(e: np.ndarray, i: int, dt: float) -> float:
    """Durée au-dessus de la moitié du pic. Un aboiement fait 80 à 400 ms ;
    une bourrasque dure bien plus, un clic bien moins."""
    demi = e[i] / 2
    a = i
    while a > 0 and e[a] > demi:
        a -= 1
    b = i
    while b < len(e) - 1 and e[b] > demi:
        b += 1
    return (b - a) * dt * 1000


def timbre(x: np.ndarray, sr: int, t: float, demi_ms: float = 150) -> tuple[float, float]:
    """(centroïde, part d'énergie au-dessus de 2 kHz).

    Un sifflement d'oiseau vit entre 2 et 8 kHz : c'est ce qui doit le
    distinguer d'un aboiement, qui est grave et large bande. ⚠️ Sur les deux
    fichiers de référence testés, cette mesure n'a RIEN séparé (1 à 6 %
    partout) — elle est là pour être contredite, pas pour être crue.
    """
    i = int(t * sr)
    a = max(0, i - int(demi_ms * sr / 1000))
    b = min(len(x), i + int(demi_ms * sr / 1000))
    seg = x[a:b]
    if len(seg) < 256:
        return 0.0, 0.0
    s = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    total = s.sum()
    if total <= 0:
        return 0.0, 0.0
    return float((fr * s).sum() / total), float(s[fr > 2000].sum() / total)


def analyse(path: Path, nom: str) -> int:
    x, sr = lire_wav(path)
    duree = len(x) / sr
    e, dt = enveloppe(x, sr)
    fond = fond_local(e, dt)
    idx = candidats(e, dt, fond)

    print(f"\n■ {nom} — {duree:.2f} s @ {sr} Hz")
    print(f"  fond médian {np.median(fond):.5f} | seuil {FACTEUR_FOND:g}× le fond local")
    if not idx:
        print("  aucun candidat")
        return 0

    niveaux = np.array([e[i] for i in idx])
    mediane = float(np.median(niveaux))

    print(f"\n  {'t':>7s} {'niveau':>8s} {'écart':>6s} {'largeur':>8s} {'>2kHz':>6s}  verdict")
    retenus = 0
    for i in idx:
        ecart = e[i] / mediane
        largeur = largeur_ms(e, i, dt)
        _, aigu = timbre(x, sr, i * dt)
        motifs = []
        if ecart > ECART_MAX:
            motifs.append(f"trop fort ({ecart:.1f}× la médiane)")
        if ecart < ECART_MIN:
            motifs.append(f"trop faible ({ecart:.2f}×)")
        if aigu > 0.25:
            motifs.append(f"aigu ({100 * aigu:.0f} %) — sifflement ?")
        if motifs:
            verdict = "REJETÉ : " + " ; ".join(motifs)
        else:
            verdict = "retenu"
            retenus += 1
        print(
            f"  {i * dt:6.2f}s {e[i]:8.4f} {ecart:6.2f} {largeur:7.0f}ms "
            f"{100 * aigu:5.0f}%  {verdict}"
        )

    print(f"\n  → {retenus} unité(s) retenue(s) sur {len(idx)} candidat(s)")
    print("  ⚠️ À VÉRIFIER À L'OREILLE. Le critère n'est pas calibré.")
    return retenus


def chemin_depuis_event(event_id: int) -> tuple[Path, str]:
    """Retrouve le WAV d'un événement. Le nom est en base, le fichier sur disque."""
    from .. import db
    from ..config import settings

    async def _chercher() -> str | None:
        await db.connect(settings.database_url)
        try:
            return await db.fetchval(
                "SELECT wav_name FROM events WHERE id = $1", event_id
            )
        finally:
            await db.disconnect()

    nom = asyncio.run(_chercher())
    if not nom:
        raise SystemExit(f"événement {event_id} introuvable, ou sans WAV")
    return settings.ondemand_dir / nom, f"événement {event_id} — {nom}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Compte les unités sonores d'une séquence.")
    ap.add_argument("wav", nargs="?", help="chemin d'un WAV")
    ap.add_argument("--event", type=int, help="identifiant d'événement (cherche son WAV)")
    args = ap.parse_args()
    if not args.wav and args.event is None:
        ap.error("donne un fichier WAV ou --event N")
    if args.event is not None:
        path, nom = chemin_depuis_event(args.event)
    else:
        path, nom = Path(args.wav), Path(args.wav).name
    if not path.exists():
        print(f"fichier introuvable : {path}", file=sys.stderr)
        return 1
    analyse(path, nom)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
