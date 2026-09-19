"""Timeline d'un fichier : ce que le modèle croit avoir entendu, tranche par tranche.

**Fonction pure.** Elle prend la matrice de scores, pas le classifieur : c'est ce
qui permet de la tester sur une matrice synthétique, sans charger le modèle, et
de garantir que le rendu ne dépend pas de la façon dont les scores ont été
obtenus.

D'où viennent les scores : `YamnetLitertBackend.score_matrix(x16)` rend une
matrice (fenêtres × 521) — exactement ce que calcule l'API YAMNet de
l'utilisateur côté Windows. Le conteneur sait donc déjà le faire seul, sans GPU
et sans transfert réseau (mesuré : 468 ms pour 52 s d'audio).

DEUX CHOSES QUE CETTE TIMELINE FAIT DIFFÉREMMENT de l'API de l'utilisateur, et
les deux sont délibérées :

1. **Elle ne remplace pas le score canin par la moyenne.** L'API de
   l'utilisateur fait `reduce_mean(scores, axis=0)` puis un seuil : un aboiement
   de 3 s dans une minute de vent se dilue à ~5 % de sa valeur, donc disparaît.
   Le projet a mesuré l'inverse (§5.4) — le critère est le MAX du groupe canin
   par fenêtre, puis le max sur les fenêtres. On garde donc `dog_max` à part, et
   il ne se calcule pas à partir de la timeline.

2. **Elle ne se repose pas sur l'`argmax`.** Une fenêtre où `Dog` marque 0,40 et
   `Speech` 0,46 s'appelle `Speech` : le chien devient invisible. On affiche
   quand même l'`argmax` — c'est ce que l'utilisateur veut lire — mais on
   expose `dog_frames` séparément, pour que l'écart entre « ce qu'il a entendu »
   et « ce qu'il a décidé » soit visible au lieu d'être enterré.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from ..audio.resample import TARGET_SR
from ..classifier.yamnet_litert import DOG_CLASS_NAMES, HOP_SAMPLES, WINDOW_SAMPLES

# ⚠️ DÉRIVÉS DES CONSTANTES DU MODÈLE, jamais écrits en dur.
#
# L'API de l'utilisateur code `hop_duration = 0.48` et `+ 0.96` en dur, parce
# que c'est le fenêtrage NATIF du YAMNet de TensorFlow Hub. Le nôtre n'est pas
# le même : le modèle LiteRT que nous embarquons attend 15 600 échantillons et
# avance de 7 800, soit 0,975 s et 0,4875 s. Recopier 0,48 ici décalerait tous
# les horodatages d'environ 0,3 s au bout d'une minute — assez pour pointer la
# mauvaise seconde, pas assez pour qu'on s'en aperçoive.
HOP_S = HOP_SAMPLES / TARGET_SR
WINDOW_S = WINDOW_SAMPLES / TARGET_SR


def dog_scores(
    matrix: np.ndarray, names: Sequence[str], dog_classes: Sequence[str] = DOG_CLASS_NAMES
) -> np.ndarray:
    """Le score canin de chaque fenêtre : le MAX sur le GROUPE, jamais l'argmax.

    Extrait ici parce que deux appelants en ont besoin — la timeline et
    l'entrée d'index qui garde les scores pour la calibration — et que deux
    copies de cette règle finiraient par diverger. Or c'est LA règle qui décide
    (§5.4) : le groupe, pas la classe `Bark`, qui s'est révélée le moins bon
    discriminateur du groupe sur les enregistrements réels.

    Le groupe est résolu PAR NOM (§3.2). Un index codé en dur se décale au
    premier changement de modèle, et le symptôme serait des scores canins
    calculés sur la mauvaise classe — plausible, donc invisible.
    """
    m = np.asarray(matrix, dtype=np.float32)
    dog_idx = [i for i, n in enumerate(names) if n in dog_classes]
    if not dog_idx or m.size == 0:
        return np.zeros(m.shape[0] if m.ndim == 2 else 0, dtype=np.float32)
    return m[:, dog_idx].max(axis=1)


def _intervalle(debut_s: float, fin_s: float) -> str:
    """`7.2s à 8.16s` — la forme de l'API de l'utilisateur.

    `round(..., 2)` sans format plutôt qu'un `:.2f` : c'est ce qui donne `7.2`
    et non `7.20`, donc exactement sa sortie.
    """
    return f"{round(debut_s, 2)}s à {round(fin_s, 2)}s"


def build_timeline(
    matrix: np.ndarray,
    names: Sequence[str],
    *,
    n_samples: int,
    min_score: float = 0.3,
    dog_threshold: float = 0.35,
    dog_classes: Sequence[str] = DOG_CLASS_NAMES,
) -> dict[str, Any]:
    """Construit la timeline d'un fichier à partir de sa matrice de scores.

    `matrix` : (fenêtres × classes), telle que rendue par `score_matrix`.
    `names`  : les noms de classes, dans l'ordre des colonnes.
    `n_samples` : nombre d'échantillons du signal, pour la durée totale.

    `min_score` filtre les fenêtres trop faibles — sans lui, une minute de vent
    produit cent fenêtres étiquetées et la timeline devient illisible. C'est le
    seuil de l'API de l'utilisateur (0,3), gardé pour que les deux sorties se
    ressemblent.

    Ne lève pas sur une matrice vide : rend une timeline vide, avec la durée.
    """
    total_s = n_samples / TARGET_SR
    vide: dict[str, Any] = {
        "total_duration_sec": round(total_s, 2),
        "frames": 0,
        "hop_s": HOP_S,
        "window_s": WINDOW_S,
        "min_score": min_score,
        "dog_threshold": dog_threshold,
        "dog_max": 0.0,
        "dog_best": None,
        "dog_frames": [],
        "timeline": [],
    }
    if matrix.size == 0:
        return vide

    m = np.asarray(matrix, dtype=np.float32)
    if m.ndim != 2 or m.shape[0] == 0:
        return vide

    canin = dog_scores(m, names, dog_classes)
    gagnants = m.argmax(axis=1)

    # --- Timeline : on FUSIONNE les fenêtres consécutives de même étiquette.
    #
    # L'API de l'utilisateur liste chaque fenêtre séparément, ce qui donne des
    # intervalles qui se chevauchent (6,83→7,80 puis 7,31→8,29 pour le même
    # sifflement). Fusionner ne perd rien — le compte de fenêtres et le score
    # max restent portés — et rend la lecture possible : « un sifflement de
    # 6,8 s à 8,3 s » au lieu de six lignes qui se recouvrent.
    timeline: list[dict[str, Any]] = []
    courant: dict[str, Any] | None = None
    for i in range(m.shape[0]):
        j = int(gagnants[i])
        score = float(m[i, j])
        if score <= min_score or names[j] == "Silence":
            courant = None
            continue
        debut = i * HOP_S
        if courant is not None and courant["sound"] == names[j]:
            courant["fin_s"] = round(debut + WINDOW_S, 2)
            courant["score"] = max(courant["score"], round(score, 4))
            courant["frames"] += 1
            courant["interval"] = _intervalle(courant["debut_s"], courant["fin_s"])
            continue
        courant = {
            "sound": names[j],
            "score": round(score, 4),
            "debut_s": round(debut, 2),
            "fin_s": round(debut + WINDOW_S, 2),
            "frames": 1,
        }
        courant["interval"] = _intervalle(courant["debut_s"], courant["fin_s"])
        timeline.append(courant)

    # --- Le canin, calculé sur le GROUPE et non sur l'argmax.
    frames_canines = [
        {
            "debut_s": round(i * HOP_S, 2),
            "fin_s": round(i * HOP_S + WINDOW_S, 2),
            "interval": _intervalle(i * HOP_S, i * HOP_S + WINDOW_S),
            "dog": round(float(canin[i]), 4),
            # Ce que l'argmax disait de cette fenêtre : c'est là qu'on voit
            # qu'un chien peut se cacher sous une autre étiquette.
            "sound": names[int(gagnants[i])],
            "sound_score": round(float(m[i, gagnants[i]]), 4),
        }
        for i in range(m.shape[0])
        if float(canin[i]) >= dog_threshold
    ]

    i_best = int(np.argmax(canin))
    dog_best = None
    if float(canin[i_best]) > 0.0:
        dog_best = {
            "debut_s": round(i_best * HOP_S, 2),
            "fin_s": round(i_best * HOP_S + WINDOW_S, 2),
            "interval": _intervalle(i_best * HOP_S, i_best * HOP_S + WINDOW_S),
            "dog": round(float(canin[i_best]), 4),
            "sound": names[int(gagnants[i_best])],
            "sound_score": round(float(m[i_best, gagnants[i_best]]), 4),
        }

    return {
        "total_duration_sec": round(total_s, 2),
        "frames": int(m.shape[0]),
        "hop_s": HOP_S,
        "window_s": WINDOW_S,
        "min_score": min_score,
        "dog_threshold": dog_threshold,
        "dog_max": round(float(canin.max()), 4),
        "dog_best": dog_best,
        "dog_frames": frames_canines,
        "timeline": timeline,
    }
