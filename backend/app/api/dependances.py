"""Dépendances partagées par les routeurs de lecture.

Une seule chose ici, mais elle porte tout le mécanisme de « vue ».

**Voir et surveiller sont deux gestes distincts.** Consulter un ancien projet ne
doit pas interrompre la campagne en cours, et l'activer demande un redémarrage
de la capture. Le projet CONSULTÉ voyage donc en paramètre de requête, et il ne
change rien à ce que la capture surveille.

Pourquoi une dépendance plutôt qu'un paramètre descendu partout : une
dépendance s'exécute **dans la même tâche** que l'endpoint, donc la variable de
contexte posée ici est visible par les helpers de `db.py` appelés ensuite. Sans
ça, il aurait fallu ajouter `projet=…` aux trente requêtes et aux onze
endpoints — et un oubli aurait affiché les données d'un autre projet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Query

from .. import db


async def projet_consulte(
    projet: int | None = Query(
        None,
        ge=1,
        description="Projet à afficher. Sans valeur : le projet actif.",
    ),
) -> AsyncIterator[None]:
    jeton = db.definir_projet_vue(projet)
    try:
        yield
    finally:
        # Remis d'aplomb même si l'endpoint lève : sinon la requête suivante
        # hériterait du projet de celle-ci.
        db.oublier_projet_vue(jeton)
