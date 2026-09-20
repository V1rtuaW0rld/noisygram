"""Test du modèle SANS navigateur — le point d'arrêt du §11.3.

    docker compose exec api python -m app.tools.selftest
    docker compose exec api python -m app.tools.selftest /data/mon_extrait.wav
    docker compose exec api python -m app.tools.selftest /data/brut.raw --rate 48000

Sans argument, il vérifie la plomberie (fichiers, class map, fenêtrage,
rééchantillonnage, latence) et passe des signaux synthétiques dont on connaît
la réponse attendue. Il NE prétend PAS prouver que le modèle reconnaît un
événement : un événement synthétique n'en est pas un.

Avec un fichier en argument, il classe de VRAIS sons. C'est le seul mode qui
réponde à la question « est-ce que ça marche ». Le test d'acceptation complet
reste le claquement de mains (doit être refusé) puis un enregistrement
d'événement (doit être accepté) — §13.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from pathlib import Path

import numpy as np

from ..audio.pcm import float32_to_pcm16, pcm16_to_float32, rms
from ..audio.resample import TARGET_SR, resample_to_16k
from ..audio.wav import read_wav_float32
from .. import projet
from ..classifier.yamnet_litert import (
    NOISY_CLASS_NAMES,
    HOP_SAMPLES,
    WINDOW_SAMPLES,
    YamnetLitertBackend,
    frame_signal,
    load_class_names,
)
from ..config import settings

# Référence : §3.1 du document de conception, et Dockerfile (qui les vérifie
# déjà au build). Dupliquées ici pour détecter un models/ monté à la main ou
# un volume qui aurait écrasé les fichiers.
EXPECTED_SHA256 = {
    "yamnet.tflite": "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf",
    "yamnet_class_map.csv": "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2",
}

EXPECTED_CLASSES = 521

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    mark = "  ok  " if ok else " ÉCHEC"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def print_top(result, limit: int = 5) -> None:
    for name, idx, score in result.top_classes[:limit]:
        bar = "█" * int(round(score * 30))
        print(f"        {score:6.3f}  {name:<38} ({idx:>3}) {bar}")


# ---------------------------------------------------------------- signaux


def sig_silence(seconds=3.0, sr=TARGET_SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def sig_white_noise(seconds=3.0, sr=TARGET_SR, dbfs=-20.0) -> np.ndarray:
    rng = np.random.default_rng(1234)  # graine fixe : test reproductible
    x = rng.standard_normal(int(seconds * sr)).astype(np.float32)
    return x / (np.max(np.abs(x)) / (10.0 ** (dbfs / 20.0)))


def sig_tone(freq=440.0, seconds=3.0, sr=TARGET_SR) -> np.ndarray:
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def sig_burst(seconds=3.0, sr=TARGET_SR) -> np.ndarray:
    """Transitoire large bande à décroissance rapide.

    C'est la FORME d'un événement, pas son contenu : YAMNet ne doit pas
    forcément y voir un événement, et ce test ne l'exige pas. Il sert à vérifier
    qu'un signal impulsionnel traverse la chaîne sans la casser.
    """
    rng = np.random.default_rng(7)
    n = int(seconds * sr)
    x = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    spec[(freqs < 250) | (freqs > 3500)] = 0  # bande d'un événement
    x = np.fft.irfft(spec, n).astype(np.float32)

    env = np.zeros(n, dtype=np.float32)
    for onset, amp, decay in ((0.30, 1.0, 22.0), (0.62, 0.7, 18.0), (0.90, 0.5, 25.0)):
        s = int(onset * sr)
        seg = np.arange(n - s, dtype=np.float32) / sr
        env[s:] = np.maximum(env[s:], amp * np.exp(-decay * seg))
    x = x * env
    peak = np.max(np.abs(x))
    return (x / peak * 0.8).astype(np.float32) if peak else x


# ---------------------------------------------------------------- entrées


def read_raw(path: Path, rate: int) -> tuple[np.ndarray, int]:
    return pcm16_to_float32(path.read_bytes(), channels=1), rate


def classify_segments(
    backend: YamnetLitertBackend, x16: np.ndarray, seconds: float
) -> list[float]:
    """Découpe en segments de `seconds` et classe chacun séparément.

    C'est la reproduction fidèle de ce que fait le client : il n'envoie jamais
    18 s d'un coup, il envoie des extraits de 3 s. Classer le fichier entier
    noierait un événement de 0,5 s dans 17,5 s de vent — ce que le MAX sur les
    fenêtres rattrape en partie, mais qui ne dit rien du comportement réel.
    """
    step = int(round(seconds * TARGET_SR))
    chunks = [x16[i : i + step] for i in range(0, x16.size, step)]
    # Une queue plus courte qu'un dixième de segment n'est pas un segment.
    if len(chunks) > 1 and chunks[-1].size < step // 10:
        chunks.pop()

    print(f"\n--- {len(chunks)} segments de {seconds:.2f} s (comme le client les envoie)")
    print(
        f"    {'#':>3}  {'début':>7}  {'NOISY':>6}  {'bark':>6}  {'moy':>6}  "
        f"{'classe dominante':<26} {'score':>5}  verdict"
    )
    print(
        "    (NOISY = score principal, max du groupe surveillé — c'est lui qui décide.\n"
        "     bark = la classe Bark seule, en diagnostic.)"
    )

    scores: list[float] = []
    barks: list[float] = []
    accepted = 0
    for i, chunk in enumerate(chunks):
        r = backend.classify(chunk)
        scores.append(r.noisy_score)
        barks.append(r.bark_score or 0.0)
        if r.noisy_score >= backend.threshold:
            accepted += 1
        top = r.top_classes[0] if r.top_classes else ("?", -1, 0.0)
        verdict = "ACCEPTÉ" if r.noisy_score >= backend.threshold else "refusé"
        print(
            f"    {i:>3}  {i * seconds:>6.1f}s  {r.noisy_score:>6.3f}  "
            f"{(r.bark_score or 0.0):>6.3f}  {r.mean_noisy_score:>6.3f}  "
            f"{top[0][:26]:<26} {top[2]:>5.2f}  {verdict}"
        )

    arr = np.asarray(scores, dtype=np.float64)
    bark_arr = np.asarray(barks, dtype=np.float64)
    print(
        f"\n    noisy : min {arr.min():.3f} | médiane {np.median(arr):.3f} | "
        f"p90 {np.percentile(arr, 90):.3f} | max {arr.max():.3f}"
    )
    print(
        f"    bark  : min {bark_arr.min():.3f} | médiane {np.median(bark_arr):.3f} "
        f"(pour comparaison des deux critères)"
    )
    print(
        f"    {accepted}/{len(chunks)} acceptés au seuil {backend.threshold:.2f}"
    )

    # Sensibilité : à quoi ressemblerait le compte pour d'autres seuils. C'est
    # la vue qui permet de choisir, plutôt qu'un chiffre magique.
    print("\n    sensibilité (segments acceptés / total) :")
    for candidate in (0.2, 0.3, 0.35, 0.5, 0.7, 0.9):
        n = int((arr >= candidate).sum())
        bar = "█" * int(round(30 * n / len(chunks)))
        mark = " ← seuil actuel" if abs(candidate - backend.threshold) < 1e-9 else ""
        print(f"      ≥ {candidate:<4} {n:>3}/{len(chunks)} {bar}{mark}")

    if accepted == len(chunks):
        print(
            "\n    ⚠ TOUS les segments passent : ce fichier ne contient que des "
            "événements.\n"
            "      Il prouve que la détection marche, il ne prouve PAS que le "
            "seuil\n"
            "      écarte les faux positifs. Pour ça il faut un enregistrement "
            "de\n"
            "      fond sonore SANS source (vent, rue, oiseaux) et le même test."
        )
    return scores


def classify_file(backend: YamnetLitertBackend, x: np.ndarray, sr: int, label: str) -> None:
    x16 = resample_to_16k(x, sr)
    duration = x16.size / TARGET_SR
    print(f"\n--- {label}")
    print(
        f"    {sr} Hz → 16 kHz | {x.size} → {x16.size} échantillons "
        f"| {duration:.2f} s | pic {float(np.max(np.abs(x16))):.3f} "
        f"| rms {rms(x16):.4f}"
    )
    result = backend.classify(x16)
    print(
        f"    {result.windows} fenêtre(s) | noisy={result.noisy_score:.3f} "
        f"bark={(result.bark_score or 0.0):.3f} moyen={result.mean_noisy_score:.3f} "
        f"| {result.processing_ms:.0f} ms"
    )
    verdict = (
        "ACCEPTÉ"
        if result.noisy_score >= backend.threshold
        else "refusé (sous le seuil)"
    )
    print(f"    seuil {backend.threshold:.2f} → {verdict}")
    print_top(result)


# ---------------------------------------------------------------- programme


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.selftest",
        description="Vérifie le pipeline audio + YAMNet sans navigateur.",
    )
    parser.add_argument("fichier", nargs="?", help="WAV, ou PCM s16le brut avec --rate")
    parser.add_argument(
        "--rate",
        type=int,
        help="fréquence du fichier si c'est du PCM brut (pas un WAV)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="avec un fichier : comparer aussi le chemin 48 kHz (test G4)",
    )
    parser.add_argument(
        "--segment",
        type=float,
        metavar="SECONDES",
        help="avec un fichier : le découper en segments de N secondes et classer "
        "chacun (reproduit ce que fait le client ; 3 = la valeur du pipeline)",
    )
    args = parser.parse_args(argv)

    print("=" * 72)
    print("  Noisygram — test du pipeline sans navigateur")
    print("=" * 72)

    # -- 1. Fichiers de modèle -------------------------------------------
    print("\n■ Fichiers")
    for name, want in EXPECTED_SHA256.items():
        path = (
            settings.model_path
            if name.endswith(".tflite")
            else settings.class_map_path
        )
        if not path.exists():
            check(f"{name} présent", False, f"absent : {path}")
            continue
        got = sha256_file(path)
        check(
            f"{name}",
            got == want,
            f"{path.stat().st_size} octets, sha256 {got[:16]}…"
            + ("" if got == want else f" ATTENDU {want[:16]}…"),
        )

    # Le projet : le groupe surveillé vient de la base, comme pour le service.
    # On le lit AVANT le class map, parce que c'est lui qui dit quelles classes
    # doivent exister — et non l'inverse.
    classes_cibles, ligne_projet = asyncio.run(projet.lire_hors_service())
    # Le seuil vient du projet, comme pour le service : mesurer avec celui de
    # `.env` donnerait des verdicts qui ne correspondent pas à la production.
    seuil_projet = (
        ligne_projet["seuil"]
        if ligne_projet and ligne_projet.get("seuil") is not None
        else settings.noisy_threshold
    )
    if ligne_projet:
        print(
            f"\n■ Projet « {ligne_projet.get('nom') or '(sans nom)'} » "
            f"→ {classes_cibles} | seuil {seuil_projet:.2f}"
        )

    # -- 2. Class map ------------------------------------------------------
    print("\n■ Class map (§3.2 — l'index 0 est Speech, PAS Animal)")
    try:
        names = load_class_names(settings.class_map_path)
        check(
            "nombre de classes",
            len(names) == EXPECTED_CLASSES,
            f"{len(names)} (attendu {EXPECTED_CLASSES})",
        )
        # Échantillon d'IDENTITÉ du modèle : quelques classes réparties dans la
        # table, pour vérifier que c'est bien ce class map-là. Volontairement
        # sans classe canine — ce test ne doit pas présupposer la cible.
        for idx, expected in (
            (0, "Speech"),
            (67, "Animal"),
            (68, "Domestic animals, pets"),
        ):
            check(
                f"index {idx} = {expected!r}",
                idx < len(names) and names[idx] == expected,
                f"lu : {names[idx]!r}" if idx < len(names) else "hors bornes",
            )
        # Le GROUPE DU PROJET doit résoudre : c'est la SEULE condition que le
        # service exige pour démarrer. Vérifier ici la présence du groupe canin
        # ferait échouer ce test sur un projet « tronçonneuse » — alors que le
        # service, lui, démarrerait très bien.
        groupe = classes_cibles or list(NOISY_CLASS_NAMES)
        manquantes = [n for n in groupe if n not in names]
        check(
            "toutes les classes du groupe surveillé existent dans le class map",
            not manquantes,
            f"absentes : {manquantes}" if manquantes else ", ".join(groupe),
        )
    except Exception as exc:  # noqa: BLE001 — on veut le rapport, pas la stack
        check("lecture du class map", False, repr(exc))

    # -- 3. Fenêtrage ------------------------------------------------------
    print("\n■ Fenêtrage (§5.3 — fenêtre 15 600, hop 7 800)")
    w3s = frame_signal(np.zeros(48_000, dtype=np.float32))
    check(
        "3 s @ 16 kHz → 5 fenêtres",
        w3s.shape == (5, WINDOW_SAMPLES),
        f"obtenu {w3s.shape}",
    )
    wshort = frame_signal(np.ones(1_000, dtype=np.float32))
    check(
        "signal court → 1 fenêtre complétée par des zéros",
        wshort.shape == (1, WINDOW_SAMPLES) and wshort[0, -1] == 0.0,
        f"obtenu {wshort.shape}",
    )
    wlong = frame_signal(np.zeros(160_000, dtype=np.float32))  # 10 s
    check(
        "10 s @ 16 kHz → 19 fenêtres",
        wlong.shape[0] == 1 + (160_000 - WINDOW_SAMPLES) // HOP_SAMPLES,
        f"obtenu {wlong.shape[0]}",
    )

    # -- 4. Rééchantillonnage ---------------------------------------------
    print("\n■ Rééchantillonnage (§5.1, risque G4)")
    for src_sr in (44_100, 48_000, 96_000):
        x = sig_tone(440.0, 3.0, src_sr)
        y = resample_to_16k(x, src_sr)
        check(
            f"{src_sr} Hz → 16 kHz",
            y.size == 48_000,
            f"{x.size} → {y.size} échantillons (attendu 48 000)",
        )
    check(
        "16 kHz inchangé",
        resample_to_16k(np.zeros(48_000, dtype=np.float32), 16_000).size == 48_000,
        "pas de rééchantillonnage inutile",
    )
    check(
        "PCM s16le aller-retour",
        np.allclose(
            pcm16_to_float32(float32_to_pcm16(sig_tone())), sig_tone(), atol=1e-4
        ),
        "aucune perte au-delà du quantum de 16 bits",
    )

    # -- 5. Chargement du modèle ------------------------------------------
    print("\n■ Chargement de YAMNet")
    backend = YamnetLitertBackend(
        model_path=settings.model_path,
        class_map_path=settings.class_map_path,
        threshold=seuil_projet,
        peak_normalize=settings.peak_normalize,
        classes_cibles=classes_cibles,
    )
    try:
        t0 = time.perf_counter()
        backend.load()
        load_ms = (time.perf_counter() - t0) * 1000.0
        check("modèle chargé", backend.is_ready(), f"{load_ms:.0f} ms")
        print(f"        {backend.describe()}")
        if settings.peak_normalize:
            print(
                "        ⚠ peak_normalize ACTIF — le point de fonctionnement est "
                "déplacé, un seuil déjà réglé ne vaut plus (G20)"
            )
    except Exception as exc:  # noqa: BLE001
        check("chargement du modèle", False, repr(exc))
        print("\nLe reste ne peut pas être testé sans modèle.")
        return 1

    # -- 6. Mode fichier réel ---------------------------------------------
    if args.fichier:
        path = Path(args.fichier)
        if not path.exists():
            print(f"\nFichier introuvable : {path}")
            return 1
        print("\n■ Fichier fourni")
        try:
            if args.rate:
                x, sr = read_raw(path, args.rate)
            else:
                x, sr = read_wav_float32(path)
        except Exception as exc:  # noqa: BLE001
            print(f"    lecture impossible : {exc!r}")
            return 1

        if args.segment:
            x16 = resample_to_16k(x, sr)
            print(
                f"\n--- {path.name} — {sr} Hz → 16 kHz | "
                f"{x.size} → {x16.size} échantillons | {x16.size / TARGET_SR:.2f} s"
            )
            classify_segments(backend, x16, args.segment)
        else:
            classify_file(backend, x, sr, f"{path.name} (fichier réel)")

        if args.compare and sr != 48_000:
            # Le même son présenté comme s'il venait d'un micro 48 kHz : c'est
            # exactement le bug G4 (transposition ~9 %) rendu visible.
            print(
                "\n--- Même signal, étiqueté 48 kHz à tort (démonstration du G4)\n"
                "    Un score qui s'effondre ici prouve que la fréquence native "
                "doit être transmise, pas supposée."
            )
            backend.classify(resample_to_16k(x, 48_000))

        backend.close()
        return _summary()

    # -- 7. Signaux synthétiques ------------------------------------------
    print("\n■ Signaux synthétiques 3 s @ 16 kHz")
    print(
        "   (le burst imite la FORME d'un événement, pas son contenu :\n"
        "    on n'exige rien de lui, il vérifie seulement que ça ne casse pas)"
    )

    results: dict[str, object] = {}
    for label, signal in (
        ("silence", sig_silence()),
        ("bruit blanc -20 dBFS", sig_white_noise()),
        ("sinusoïde 440 Hz", sig_tone()),
        ("burst large bande", sig_burst()),
    ):
        results[label] = backend.classify(signal)
        classify_file(backend, signal, TARGET_SR, label)

    print("\n■ Attendu / obtenu")
    sil = results["silence"]
    noise = results["bruit blanc -20 dBFS"]
    check(
        "silence → score quasi nul",
        sil.noisy_score < 0.05,
        f"noisy={sil.noisy_score:.4f}",
    )
    check(
        "bruit blanc → score sous le seuil",
        noise.noisy_score < backend.threshold,
        f"noisy={noise.noisy_score:.4f} (seuil {backend.threshold})",
    )

    all_results = list(results.items())
    finite = all(
        np.isfinite(r.noisy_score) and 0.0 <= r.noisy_score <= 1.0 for _, r in all_results
    )
    check("tous les scores finis et dans [0,1]", finite)

    latencies = [r.processing_ms for _, r in all_results]
    check(
        "latence par segment < 2000 ms",
        max(latencies) < 2000.0,
        f"max {max(latencies):.0f} ms "
        f"(marge backpressure : max_pending={settings.max_pending})",
    )

    print(
        "\n⚠ Ce test ne prouve PAS la détection d'événement. Pour ça, il faut un "
        "VRAI son :\n"
        "    docker compose cp extrait.wav api:/data/\n"
        "    docker compose exec api python -m app.tools.selftest /data/extrait.wav"
    )

    backend.close()
    return _summary()


def _summary() -> int:
    print("\n" + "=" * 72)
    if _failures:
        print(f"  {len(_failures)} ÉCHEC(S) sur {_checks} vérifications :")
        for f in _failures:
            print(f"    - {f}")
        print("=" * 72)
        return 1
    print(f"  {_checks} vérifications, toutes OK")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
