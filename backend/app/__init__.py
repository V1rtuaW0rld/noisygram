"""Noisygram — détection et historisation d'événements.

Architecture hybride : un client navigateur fait le pré-tri (détection RMS,
très peu de CPU) et n'envoie que les extraits candidats ; ce serveur fait la
qualification (YAMNet) et l'historisation. On ne streame jamais d'audio en
permanence.
"""

__version__ = "0.1.0"
