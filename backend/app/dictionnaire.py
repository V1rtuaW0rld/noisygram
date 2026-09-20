"""Terme en français ou en anglais → classes YAMNet.

C'est le cerveau de la première voie de la modale projet : l'utilisateur tape
« aboiement » ou « chainsaw », et on lui propose **la liste de classes YAMNet
qui s'y rapportent**, qu'il peut ensuite corriger.

⚠️ **Aucun nom de classe n'est inventé.** Chacun a été relevé dans
`yamnet_class_map.csv` — les 521 classes du modèle. Un seul caractère faux, et
la classe ne résout plus au chargement : le groupe est silencieusement plus
étroit que voulu, et la détection rate. `valider()` vérifie cet invariant, et
`tests` l'exerce.

⚠️ Ce n'est PAS un modèle de langage. C'est une table de correspondance. Pour un
vocabulaire fermé de 521 étiquettes, un dictionnaire de quelques kilo-octets
fait le travail qu'un traducteur ferait moins bien et cent fois plus cher.

Les libellés anglais sont ceux du modèle, et ils restent en anglais : c'est ce
que YAMNet comprend. Le français n'est qu'une porte d'entrée.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable

# Familles de bruit domestique et urbain. Chacune : des mots d'entrée (français
# ET anglais), et les classes YAMNet correspondantes.
#
# `elargir` marque les familles où la classe « Animal » voisine est délibérément
# EXCLUE — elle réagit aux chats, aux oiseaux et au bétail. C'est la décision du
# document de conception, mesurée sur le terrain.
FAMILLES: list[dict] = [
    {
        "id": "aboiement",
        "mots": ["aboiement", "aboiements", "aboyer", "chien", "chiens", "canin",
                 "ouaf", "wouf", "wouaf",
                 "bark", "barking", "dog", "dogs", "howl", "growl", "woof"],
        "classes": ["Dog", "Bark", "Yip", "Howl", "Bow-wow", "Growling", "Whimper (dog)"],
    },
    {
        "id": "chat",
        "mots": ["chat", "chats", "miaulement", "miauler", "miaou", "ronron",
                 "ronronnement",
                 "cat", "cats", "meow", "purr", "caterwaul"],
        "classes": ["Cat", "Meow", "Purr", "Hiss", "Caterwaul"],
    },
    {
        "id": "oiseau",
        "mots": ["oiseau", "oiseaux", "chant d'oiseau", "gazouillis", "piaillement",
                 "bird", "birds", "chirp", "tweet", "birdsong"],
        "classes": ["Bird", "Bird vocalization, bird call, bird song", "Chirp, tweet",
                    "Squawk", "Crow", "Owl", "Pigeon, dove"],
    },
    {
        "id": "tronconneuse",
        "mots": ["tronconneuse", "tronçonneuse", "chainsaw", "chain saw"],
        "classes": ["Chainsaw"],
    },
    {
        "id": "tondeuse",
        "mots": ["tondeuse", "tondeuse a gazon", "lawn mower", "lawnmower", "mower"],
        "classes": ["Lawn mower"],
    },
    {
        "id": "ronflement",
        "mots": ["ronflement", "ronflements", "ronfler", "somnolence",
                 "snore", "snoring", "snort", "wheeze"],
        "classes": ["Snoring", "Snort", "Wheeze", "Breathing"],
    },
    {
        "id": "klaxon",
        "mots": ["klaxon", "klaxons", "avertisseur", "coup de klaxon",
                 "horn", "honk", "honking", "car horn"],
        "classes": ["Vehicle horn, car horn, honking", "Air horn, truck horn",
                    "Honk", "Foghorn", "Train horn"],
    },
    {
        "id": "moteur",
        "mots": ["moteur", "moteurs", "camion", "moto", "voiture", "circulation",
                 "engine", "motor", "truck", "motorcycle", "traffic", "idling"],
        "classes": ["Engine", "Heavy engine (low frequency)", "Medium engine (mid frequency)",
                    "Light engine (high frequency)", "Idling", "Motor vehicle (road)",
                    "Truck", "Motorcycle", "Bus", "Car passing by"],
    },
    {
        "id": "sonnette",
        "mots": ["sonnette", "sonnerie", "interphone", "on a sonne", "carillon",
                 "doorbell", "bell", "ding dong", "buzzer"],
        "classes": ["Doorbell", "Ding-dong", "Ding", "Buzzer",
                    "Telephone bell ringing", "Bicycle bell"],
    },
    {
        "id": "porte",
        "mots": ["porte", "claquement de porte", "frapper a la porte",
                 "door", "knock", "sliding door"],
        "classes": ["Door", "Sliding door", "Knock"],
    },
    {
        "id": "marteau",
        "mots": ["marteau", "marteau piqueur", "coups de marteau", "travaux",
                 "hammer", "jackhammer", "pneumatic drill"],
        "classes": ["Hammer", "Jackhammer"],
    },
    {
        "id": "outillage",
        "mots": ["perceuse", "scie", "ponceuse", "outil", "bricolage",
                 "drill", "saw", "sanding", "power tool"],
        "classes": ["Drill", "Power tool", "Sawing", "Sanding"],
    },
    {
        "id": "alarme",
        "mots": ["alarme", "alerte", "detecteur de fumee", "reveil",
                 "alarm", "smoke detector", "alarm clock"],
        "classes": ["Alarm", "Smoke detector, smoke alarm", "Fire alarm",
                    "Car alarm", "Alarm clock", "Beep, bleep"],
    },
    {
        "id": "sirene",
        "mots": ["sirene", "pompiers", "police", "ambulance", "urgence",
                 "siren", "emergency"],
        "classes": ["Siren", "Police car (siren)", "Ambulance (siren)",
                    "Fire engine, fire truck (siren)", "Civil defense siren",
                    "Emergency vehicle", "Reversing beeps"],
    },
    {
        "id": "coup de feu",
        "mots": ["coup de feu", "fusil", "tir", "arme", "petard", "feu d'artifice",
                 "gunshot", "gunfire", "explosion", "firework", "firecracker"],
        "classes": ["Gunshot, gunfire", "Explosion", "Machine gun", "Cap gun",
                    "Fireworks", "Firecracker", "Artillery fire"],
    },
    {
        "id": "verre brise",
        "mots": ["verre brise", "bris de verre", "vitre", "casse",
                 "glass", "shatter", "smash", "breaking"],
        "classes": ["Glass", "Shatter", "Smash, crash", "Breaking"],
    },
    {
        "id": "aspirateur",
        "mots": ["aspirateur", "menage", "vaisselle",
                 "vacuum", "vacuum cleaner", "dishes"],
        "classes": ["Vacuum cleaner", "Dishes, pots, and pans"],
    },
    {
        "id": "eau",
        "mots": ["eau", "robinet", "fuite", "ruisseau", "pluie",
                 "water", "faucet", "tap", "stream", "rain"],
        "classes": ["Water", "Water tap, faucet", "Stream", "Rain", "Raindrop",
                    "Rain on surface", "Waterfall"],
    },
    {
        "id": "vent",
        "mots": ["vent", "bourrasque", "feuilles", "orage", "tonnerre",
                 "wind", "rustling", "thunder", "storm"],
        "classes": ["Wind", "Wind noise (microphone)", "Rustling leaves",
                    "Rustle", "Thunder", "Thunderstorm"],
    },
    {
        "id": "parole",
        "mots": ["parole", "voix", "conversation", "discussion", "brouhaha",
                 "speech", "voice", "conversation", "talking"],
        "classes": ["Speech", "Conversation", "Hubbub, speech noise, speech babble",
                    "Child speech, kid speaking", "Shout", "Whistling"],
    },
    {
        "id": "rire",
        "mots": ["rire", "rires", "fou rire", "laugh", "laughter", "giggle"],
        "classes": ["Laughter", "Belly laugh", "Baby laughter"],
    },
    {
        "id": "toux",
        "mots": ["toux", "tousser", "eternuement", "eternuer",
                 "cough", "sneeze"],
        "classes": ["Cough", "Sneeze"],
    },
    {
        "id": "pas",
        "mots": ["pas", "bruits de pas", "marche", "footsteps", "walking"],
        "classes": ["Walk, footsteps"],
    },
    {
        "id": "cloche",
        "mots": ["cloche", "cloches", "carillon d'eglise", "bell", "church bell"],
        "classes": ["Bell", "Church bell", "Jingle bell", "Tubular bells"],
    },
    {
        "id": "train",
        "mots": ["train", "locomotive", "gare", "rail", "railway"],
        "classes": ["Train", "Train horn", "Train whistle", "Railroad car, train wagon",
                    "Train wheels squealing"],
    },
    {
        "id": "avion",
        "mots": ["avion", "reacteur", "aeroport", "plane", "aircraft", "jet"],
        "classes": ["Aircraft engine", "Jet engine"],
    },
    {
        "id": "musique",
        "mots": ["musique", "radio", "television", "music", "singing"],
        "classes": ["Music", "Singing", "Musical instrument"],
    },
]


def _normalise(texte: str) -> str:
    """Minuscules, sans accents, espaces réduits.

    « Tronçonneuse » et « tronconneuse » doivent trouver la même famille : on ne
    peut pas demander à quelqu'un de taper les accents depuis un téléphone.
    """
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def proposer(terme: str) -> list[dict]:
    """Les familles qui correspondent au terme, la meilleure d'abord.

    Correspondance par mot ENTIER pour les termes courts (« cat » ne doit pas
    matcher « caterwaul » dans une autre famille), et par sous-chaîne pour les
    termes longs — « tronçonneuse » doit trouver « tronconneuse ».
    """
    cible = _normalise(terme)
    if not cible:
        return []

    resultats: list[tuple[int, dict]] = []
    for fam in FAMILLES:
        score = 0
        for mot in fam["mots"]:
            m = _normalise(mot)
            if not m:
                continue
            if m == cible:
                score = max(score, 3)
            elif len(m) >= 5 and (m in cible or cible in m):
                score = max(score, 2)
            elif len(m) >= 3 and m in cible.split():
                score = max(score, 1)
        if score:
            resultats.append((score, fam))

    resultats.sort(key=lambda p: (-p[0], p[1]["id"]))
    return [
        {"id": f["id"], "classes": list(f["classes"]), "score": s}
        for s, f in resultats
    ]


def classes_pour(terme: str) -> list[str]:
    """Les classes de la meilleure famille, ou une liste vide."""
    p = proposer(terme)
    return p[0]["classes"] if p else []


def toutes_les_classes() -> set[str]:
    """Toutes les classes citées, pour la validation."""
    return {c for f in FAMILLES for c in f["classes"]}


def valider(noms_du_modele: Iterable[str]) -> list[str]:
    """Les classes citées qui N'EXISTENT PAS dans le class map.

    À appeler au démarrage ou depuis un test : une seule faute de frappe, et la
    classe ne résout plus au chargement — le groupe devient silencieusement plus
    étroit, et la détection rate sans le dire.
    """
    connues = set(noms_du_modele)
    return sorted(c for c in toutes_les_classes() if c not in connues)
