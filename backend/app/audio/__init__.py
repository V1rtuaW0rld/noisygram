"""Traitement audio — PCM brut vers YAMNet, sans ffmpeg (§3.4).

Le client envoie du PCM s16le à sa fréquence NATIVE. Ce paquet ne fait donc
que trois choses : convertir, rééchantillonner, encoder en MP3.
"""
