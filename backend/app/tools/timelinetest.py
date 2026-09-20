"""Test de la timeline d'analyse — sans modèle, et sans fichier si on veut.

    docker compose exec capture python -m app.tools.timelinetest
    docker compose exec capture python -m app.tools.timelinetest /data/debug/x.wav

Sans argument, il vérifie la LOGIQUE PURE sur des matrices synthétiques : c'est
le mode qui doit tourner à chaque modification, et il ne charge ni YAMNet ni
quoi que ce soit d'autre. Avec un fichier, il affiche en plus la vraie timeline.

Ce qui est vérifié ici ne se voit pas à la lecture, et c'est pour ça que ça
mérite un test :

  · une fenêtre où `Dog` marque 0,40 et `Speech` 0,46 s'appelle `Speech` — la
    source doit rester visible dans `noisy_frames`, sinon elle disparaît ;
  · les fenêtres consécutives de même étiquette fusionnent, et la fusion garde
    le MEILLEUR score, pas le dernier ;
  · la grille est celle du modèle (0,4875 s), jamais 0,48 recopié de l'API
    YAMNet de TensorFlow Hub — l'écart ne serait visible qu'au bout d'une
    minute, et ressemblerait à une erreur de quelques dixièmes.
"""

from __future__ import annotations

import sys

import numpy as np

from ..analysis.timeline import HOP_S, WINDOW_S, build_timeline, noisy_scores
from ..classifier.yamnet_litert import NOISY_CLASS_NAMES

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    print(f"[{'  ok  ' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def test_pur() -> None:
    """La logique, sur des matrices écrites à la main."""
    noms = ["Whistling", "Dog", "Speech"]
    m = np.array(
        [
            [0.90, 0.10, 0.05],  # Whistling
            [0.50, 0.20, 0.10],  # Whistling — doit FUSIONNER avec la précédente
            [0.10, 0.40, 0.46],  # argmax Speech, mais Dog à 0.40 : la source doit rester vue
            [0.05, 0.05, 0.02],  # sous le seuil : troue la timeline
            [0.00, 0.00, 0.00],
        ],
        dtype=np.float32,
    )
    # Le groupe est passé EXPLICITEMENT : cette fixture teste le MÉCANISME
    # « une fenêtre dont l'argmax est Speech mais dont la classe surveillée
    # marque 0,40 reste visible », pas la cible du projet. S'appuyer sur le
    # défaut ferait changer le sens du test le jour où le défaut change.
    r = build_timeline(
        m, noms, n_samples=5 * 15600, min_score=0.3, noisy_threshold=0.35,
        noisy_classes=["Dog"],
    )

    check("deux segments après fusion", len(r["timeline"]) == 2,
          str([s["sound"] for s in r["timeline"]]))

    s0 = r["timeline"][0] if r["timeline"] else {}
    check("le premier fusionne deux fenêtres",
          s0.get("sound") == "Whistling" and s0.get("frames") == 2,
          f"{s0.get('sound')} x{s0.get('frames')}")
    check("la fusion garde le MEILLEUR score", s0.get("score") == 0.9, str(s0.get("score")))
    check("l'intervalle fusionné est juste", s0.get("interval") == "0.0s à 1.46s",
          str(s0.get("interval")))

    check("la source est vue MALGRÉ l'argmax", len(r["noisy_frames"]) == 1,
          f"{len(r['noisy_frames'])} fenêtre(s)")
    if r["noisy_frames"]:
        df = r["noisy_frames"][0]
        check("et l'écart est nommé, pas caché",
              df["sound"] == "Speech" and df["noisy"] == 0.4,
              f"noisy {df['noisy']} vs {df['sound']} {df['sound_score']}")

    check("noisy_max vient du GROUPE, pas de la timeline", r["noisy_max"] == 0.4, str(r["noisy_max"]))

    # La grille. Le piège : 0,48 est écrit en dur dans l'API YAMNet de TF Hub,
    # et le recopier décale tous les horodatages.
    check("hop_s dérivé du modèle, jamais 0.48", abs(HOP_S - 0.4875) < 1e-9, str(HOP_S))
    check("window_s dérivé du modèle, jamais 0.96", abs(WINDOW_S - 0.975) < 1e-9, str(WINDOW_S))

    # Le groupe surveillé se résout par NOM : une matrice où aucun nom ne
    # correspond doit rendre des zéros, pas lever ni prendre la colonne 0.
    zeros = noisy_scores(m, ["Rien", "Ailleurs", "Aucun"])
    check("groupe surveillé introuvable → zéros, sans lever",
          zeros.shape == (5,) and not zeros.any())

    vide = build_timeline(np.zeros((0, 0), dtype=np.float32), noms, n_samples=0)
    check("matrice vide → timeline vide, sans lever",
          vide["timeline"] == [] and vide["frames"] == 0)

    # Un signal plus court qu'une fenêtre donne UNE fenêtre : c'est
    # `frame_signal` qui complète par des zéros, et la timeline doit le suivre
    # sans inventer d'intervalle négatif.
    court = build_timeline(np.array([[0.9, 0.0, 0.0]], dtype=np.float32), noms, n_samples=8000)
    check("une seule fenêtre → un seul segment", len(court["timeline"]) == 1,
          str(court["timeline"]))


def test_reel(chemin: str) -> None:
    """La vraie chaîne, sur un fichier fourni."""
    from .. import projet
    from ..audio.resample import resample_to_16k
    from ..audio.wav import read_wav_float32
    from ..classifier.factory import build_classifier
    from ..classifier.yamnet_litert import load_class_names
    from ..config import settings
    import asyncio
    import time

    x, sr = read_wav_float32(chemin)
    x16 = resample_to_16k(x, sr)
    # Même raison que dans selftest : sans la config, cet outil afficherait le
    # groupe PAR DÉFAUT pendant que le service en surveille un autre.
    classes_cibles, projet_courant = asyncio.run(projet.lire_hors_service())
    seuil = (
        projet_courant["seuil"]
        if projet_courant and projet_courant.get("seuil") is not None
        else settings.noisy_threshold
    )
    clf = build_classifier(settings, classes_cibles, seuil)
    clf.load()
    noms = load_class_names(settings.class_map_path)

    t0 = time.monotonic()
    m = clf.score_matrix(x16)
    dt = time.monotonic() - t0
    r = build_timeline(
        m, noms, n_samples=x16.size,
        min_score=settings.analyze_timeline_min_score,
        # Le seuil du PROJET, comme le classifieur : la timeline doit expliquer
        # les scores qui viennent d'être calculés.
        noisy_threshold=seuil,
        # Le groupe du classifieur EN SERVICE : la timeline doit expliquer les
        # scores qui viennent d'être calculés, pas ceux d'un autre groupe.
        noisy_classes=list(clf.classes_cibles),
    )

    duree = x16.size / 16000
    print(f"\n  {chemin}")
    print(f"  {duree:.1f} s, {r['frames']} fenêtres, matrice en {dt*1000:.0f} ms "
          f"({dt/duree*1000:.1f} ms par seconde d'audio)")
    print(f"  score max {r['noisy_max']} — seuil {r['noisy_threshold']}, "
          f"{len(r['noisy_frames'])} fenêtre(s) retenue(s)")
    for s in r["timeline"]:
        print(f"    {s['interval']:<24} {s['sound']:<28} {s['score']}")
    if r["noisy_best"]:
        b = r["noisy_best"]
        print(f"  meilleur moment : {b['interval']} → {b['noisy']} "
              f"(étiquette dominante « {b['sound']} » {b['sound_score']})")

    check("la matrice a une ligne par fenêtre",
          m.shape[0] == r["frames"], f"{m.shape[0]} vs {r['frames']}")
    check("le score surveillé atteint bien le seuil ou pas, mais est calculé",
          r["noisy_max"] >= 0.0 and (r["noisy_frames"] == []) == (r["noisy_max"] < r["noisy_threshold"]),
          f"max {r['noisy_max']} / {len(r['noisy_frames'])} fenêtre(s)")
    check("les segments sont ordonnés",
          all(r["timeline"][i]["debut_s"] <= r["timeline"][i + 1]["debut_s"]
              for i in range(len(r["timeline"]) - 1)))


def main() -> int:
    print("■ Timeline d'analyse — logique pure")
    # Le groupe vient du PROJET. On le lit pour l'afficher, mais son absence ne
    # doit pas empêcher le test PUR de tourner : celui-ci ne touche pas à la
    # base et n'a pas besoin d'un projet configuré.
    try:
        import asyncio

        from .. import projet

        classes, ligne = asyncio.run(projet.lire_hors_service())
        nom = (ligne or {}).get("nom")
        print(
            "  groupe surveillé : "
            + (f"{list(classes)}  (projet « {nom or 'sans nom'} »)" if classes
               else "(aucun projet — le backend appliquera son défaut)")
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  groupe surveillé : (projet illisible — {exc})")
    test_pur()

    if len(sys.argv) > 1:
        test_reel(sys.argv[1])

    print()
    if _failures:
        print(f"  {len(_failures)} ÉCHEC(S) sur {_checks} vérifications : "
              + " ; ".join(_failures))
        return 1
    print(f"  {_checks} vérifications, toutes OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
