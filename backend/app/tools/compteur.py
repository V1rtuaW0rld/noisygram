"""Compte les unités sonores dans une séquence — les aboiements d'une rafale.

    docker compose exec capture python -m app.tools.compteur --event 541
    docker compose exec capture python -m app.tools.compteur /data/debug/x.wav

═══════════════════════════════════════════════════════════════════════════
LE POINT QUI A TOUT CHANGÉ : CE N'EST PAS L'AMPLITUDE, C'EST LA FRÉQUENCE.

Pendant longtemps cet outil comptait les pics de l'enveloppe **pleine bande**.
C'était faux, et de loin : sur 480 il trouvait 24 pics pour 5 aboiements, sur
507 41 pour 5. Il comptait le vent.

Le vent sous 1 kHz est **quasi inaudible** mais produit des excursions énormes
sur la membrane du micro : en amplitude, il écrase tout. L'oreille, elle, ne
l'entend pas — d'où l'impression trompeuse que « l'aboiement gagne toujours ».

C'est en regardant les SPECTROGRAMMES qu'on l'a vu :
  · le vent forme une nappe horizontale sous ~800 Hz ;
  · un aboiement est une colonne verticale large, qui monte au-dessus de 1 kHz ;
  · un oiseau est un trait fin et ondulant, dans les aigus.

En prenant l'enveloppe de la seule bande **1000–8000 Hz**, les comptes tombent :
    480 : 24 -> 5 (vérité 5)      507 : 41 -> 6 (vérité 5)
    483 : 15 -> 4 (vérité 4)      541 :  4 -> 4 (vérité 4)
Soit 7 fichiers exacts sur 9, contre 4 avant — et les deux écarts restants sont
de +1 et +4, là où c'étaient des facteurs 3 à 7.

CE QUI RESTE FAUX, ET QU'IL FAUT SAVOIR
- 507 : 6 au lieu de 5, 546 : 6 au lieu de 2. Sur 546 l'utilisateur signale des
  oiseaux ; ils vivent aussi au-dessus de 1 kHz, donc la bande ne les écarte pas.
- J'ai tenté de les séparer par la PLATITUDE spectrale (bruit large bande contre
  trait pur). **Ça ne marche pas** : les quatre vrais aboiements de 483 ont la
  platitude la plus basse du fichier. Mesure non fiable, piste non concluante.
- Le seuil à 4× le fond local et le réfractaire de 120 ms ne sont PAS calibrés
  finement : ils n'ont jamais été ajustés que sur neuf fichiers.

⚠️ Le compte rendu est une PROPOSITION à vérifier à l'oreille, pas un résultat.
Et il n'est pas écrit en base : une colonne remplie par un critère non calibré
donne des chiffres qui ont l'air d'autorité.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import numpy as np

# --- Réglages --------------------------------------------------------------
# La bande est le seul réglage qui a été VÉRIFIÉ. Les autres sont des points de
# départ, ajustés sur neuf fichiers seulement.
BANDE_BASSE = 1000.0  # sous cette fréquence, c'est du vent
BANDE_HAUTE = 8000.0  # au-delà, il n'y a plus rien d'exploitable
FEN_MS = 20.0  # largeur de trame de l'enveloppe
HOP_MS = 10.0  # pas : 10 ms de résolution, contre 487 ms pour le score YAMNet
FACTEUR_FOND = 4.0  # un pic doit valoir ça de fois le fond local
REFRACT_MS = 120.0  # deux aboiements ne peuvent pas être plus proches


def lire_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def enveloppe_bande(
    x: np.ndarray,
    sr: int,
    fmin: float = BANDE_BASSE,
    fmax: float = BANDE_HAUTE,
) -> tuple[np.ndarray, float]:
    """RMS par trame, DANS UNE BANDE de fréquences.

    On passe par une FFT glissante plutôt que par un filtre : un filtre à
    réponse impulsionnelle longue (Butterworth) **invente des pics** par
    ringing, ce qui est précisément ce qu'on cherche à ne pas faire ici.
    """
    f, h = int(FEN_MS * sr / 1000), int(HOP_MS * sr / 1000)
    win = np.hanning(f)
    fr = np.fft.rfftfreq(f, 1 / sr)
    bande = (fr >= fmin) & (fr < fmax)
    n = 1 + max(0, (len(x) - f) // h)
    e = np.empty(n)
    for k in range(n):
        s = np.abs(np.fft.rfft(x[k * h : k * h + f] * win))
        e[k] = np.sqrt((s[bande] ** 2).sum())
    return np.convolve(e, np.ones(3) / 3, mode="same"), h / sr


def fond_local(e: np.ndarray, dt: float, fen_s: float = 1.5) -> np.ndarray:
    """Médiane glissante : le fond bouge, un fond global ferait passer une
    bourrasque pour une salve."""
    demi = max(1, int(fen_s / 2 / dt))
    return np.array([np.median(e[max(0, i - demi) : i + demi + 1]) for i in range(len(e))])


def candidats(e: np.ndarray, dt: float, fond: np.ndarray) -> list[int]:
    seuil = FACTEUR_FOND * fond
    refr = max(1, int(REFRACT_MS / 1000 / dt))
    pics = [
        i
        for i in range(1, len(e) - 1)
        if e[i] >= e[i - 1] and e[i] > e[i + 1] and e[i] > seuil[i]
    ]
    gardes: list[int] = []
    for i in pics:
        if not gardes or i - gardes[-1] >= refr:
            gardes.append(i)
        elif e[i] > e[gardes[-1]]:
            gardes[-1] = i  # on garde le plus fort du groupe
    return gardes


def largeur_ms(e: np.ndarray, i: int, dt: float) -> float:
    """Durée au-dessus de la moitié du pic. Information de forme, pas critère."""
    demi = e[i] / 2
    a = i
    while a > 0 and e[a] > demi:
        a -= 1
    b = i
    while b < len(e) - 1 and e[b] > demi:
        b += 1
    return (b - a) * dt * 1000


def analyse(path: Path, nom: str) -> int:
    x, sr = lire_wav(path)
    e, dt = enveloppe_bande(x, sr)
    fond = fond_local(e, dt)
    idx = candidats(e, dt, fond)

    print(f"\n■ {nom} — {len(x) / sr:.2f} s @ {sr} Hz")
    print(
        f"  bande {BANDE_BASSE:.0f}-{BANDE_HAUTE:.0f} Hz | seuil {FACTEUR_FOND:g}× "
        f"le fond local | réfractaire {REFRACT_MS:.0f} ms"
    )
    if not idx:
        print("  aucun candidat")
        return 0

    print(f"\n  {'n°':>3s} {'t':>7s} {'niveau':>9s} {'largeur':>8s}")
    for k, i in enumerate(idx, 1):
        print(f"  {k:3d} {i * dt:6.2f}s {e[i]:9.5f} {largeur_ms(e, i, dt):7.0f}ms")

    print(f"\n  → {len(idx)} unité(s) proposée(s)")
    print("  ⚠️ À VÉRIFIER À L'OREILLE. Le critère n'est pas calibré finement.")
    print("     Connu faux sur 546 (oiseaux) et approché sur 507 (+1).")
    return len(idx)


def chemin_depuis_event(event_id: int) -> tuple[Path, str]:
    from .. import db
    from ..config import settings

    async def _chercher() -> str | None:
        await db.connect(settings.database_url)
        try:
            return await db.fetchval("SELECT wav_name FROM events WHERE id = $1", event_id)
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
