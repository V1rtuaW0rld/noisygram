"""Ce que cette capture surveille, et comment elle suit un changement.

Le projet actif vit en base et change quand on l'active depuis le dashboard.
Le groupe de classes surveillé, lui, n'est **pas gravé dans le modèle** : c'est
une sélection de colonnes dans les 521 sorties de YAMNet, plus un seuil. Le
suivre ne demande donc aucun redémarrage — c'est ce module qui l'a établi, et
c'est cette phrase-là qui a coûté un `docker compose restart capture` pour rien
tant qu'elle n'était pas vérifiée dans le code.

Il n'y a **aucune tâche de fond** ici, et ce n'est pas un oubli : `suivre()` est
appelé à chaque `hello` et à chaque `ping` du poste de terrain. Sans poste
connecté il n'y a pas d'audio à classer, donc rien à resynchroniser — la
connexion d'un poste est précisément le moment où le groupe compte, et elle
resynchronise toujours.
"""

from __future__ import annotations

import asyncio
import logging

from . import projet
from .ws.protocol import projet_change

log = logging.getLogger(__name__)


class Surveillance:
    """Le projet que cette capture applique, et la bascule vers le suivant.

    Vit sur `app.state.surveillance` : une seule instance par processus, comme
    le classifieur — elle porte l'état partagé entre sessions.
    """

    def __init__(self, classifier, hub=None) -> None:
        self.classifier = classifier
        # Le hub peut être None (outils de test qui montent l'application sans
        # passer par le lifespan) : le suivi doit continuer à fonctionner sans
        # personne à prévenir.
        self.hub = hub
        # Plusieurs postes pinguent en même temps : sans ce verrou, deux
        # `suivre()` concurrents liraient tous les deux l'ancien état et
        # pousseraient le même changement deux fois.
        self._lock = asyncio.Lock()
        self._applique: dict | None = None
        # Le dernier projet REFUSÉ, et la clé qui l'a fait refuser. Mémorisé
        # pour ne pas repousser le même avertissement à chaque ping : un projet
        # mal configuré noierait le journal du poste sous une ligne identique
        # toutes les quinze secondes, et l'opérateur cesserait de la lire.
        self._refus: tuple[tuple, str] | None = None

    # ------------------------------------------------------------- lecture

    @property
    def nom(self) -> str | None:
        """Le projet appliqué, ou None tant qu'aucun ne l'est."""
        return self._applique["nom"] if self._applique else None

    @property
    def applique(self) -> dict | None:
        return self._applique

    def poser(self, courant: dict | None) -> None:
        """Mémorise le projet DÉJÀ appliqué au démarrage, sans rien échanger.

        Appelé par le `lifespan`, qui vient de construire le classifieur sur ce
        projet-là. Sans ça, le premier `suivre()` croirait à un changement et
        annoncerait au poste une bascule qui n'a pas eu lieu.
        """
        self._applique = courant
        self._refus = None

    @staticmethod
    def _cle(courant: dict) -> tuple:
        """L'identité d'un projet, par CONTENU.

        Pas par `updated_at` : c'est le contenu qui décide de ce que la capture
        surveille, et un renommage ou un seuil corrigé doivent déclencher autant
        qu'une activation. Comparer une date marcherait aujourd'hui et
        cesserait de marcher le jour où une écriture oublierait de la poser —
        en silence, comme toujours.
        """
        return (courant["id"], courant["nom"], tuple(courant["classes"]), courant["seuil"])

    # ------------------------------------------------------------ bascule

    async def suivre(self) -> dict | None:
        """Relit le projet actif et applique ce qui a changé.

        Rend le message `projet_change` si quelque chose a bougé — appliqué ou
        refusé — et `None` sinon. Idempotent : appelé à chaque ping, il ne fait
        le plus souvent rien du tout.
        """
        async with self._lock:
            courant = await projet.actif()
            if courant is None:
                # ⚠️ `actif()` rend None AUSSI quand la base est injoignable :
                # il avale l'exception et se contente d'un avertissement. Le
                # traduire en « plus de projet » viderait le groupe surveillé à
                # la première hoquet, et la capture cesserait de compter sans
                # que rien ne le dise. On ne touche donc à rien.
                log.debug("projet actif illisible — groupe conservé")
                return None

            cle = self._cle(courant)
            if self._applique is not None and cle == self._cle(self._applique):
                return None
            if self._refus is not None and self._refus[0] == cle:
                return None

            try:
                self.classifier.reconfigurer(courant["classes"], courant["seuil"])
            except Exception as exc:  # noqa: BLE001
                raison = str(exc)
                self._refus = (cle, raison)
                log.error(
                    "projet « %s » REFUSÉ : %s — la capture continue de compter "
                    "« %s »",
                    courant["nom"],
                    raison,
                    self.nom,
                )
                # L'ancien groupe reste en place. Appliquer à moitié donnerait
                # une capture qui compte autre chose que ce qu'elle croit
                # compter — le pire des deux mondes.
                message = projet_change(
                    projet=courant["nom"],
                    classes=courant["classes"],
                    seuil=courant["seuil"],
                    applique=False,
                    raison=raison,
                )
                await self._diffuser(message)
                return message

            self._applique = courant
            self._refus = None
            log.info(
                "projet appliqué à chaud : « %s », %d classe(s), seuil %.2f",
                courant["nom"],
                len(courant["classes"]),
                courant["seuil"] or 0.0,
            )
            message = projet_change(
                projet=courant["nom"],
                classes=courant["classes"],
                seuil=courant["seuil"],
                applique=True,
                raison=None,
            )
            await self._diffuser(message)
            return message

    async def _diffuser(self, message: dict) -> None:
        """Porte le changement aux postes connectés.

        `hub.broadcast` ne convient pas : il va aux AUDITEURS de l'écoute
        directe, pas aux postes de terrain. Ce sont deux publics distincts, et
        l'un n'a que faire du groupe que la capture surveille.
        """
        if self.hub is None:
            return
        for session in self.hub.postes():
            await session.notifier(message)
