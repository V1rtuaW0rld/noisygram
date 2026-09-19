"""Qualification des extraits.

`base.ClassifierBackend` est l'interface ; le reste est branchable. YAMNet est
le backend par défaut et le seul sélectionné, mais le pont GPU distant
(`remote_http.py`) est implémenté pour qu'un futur passage à un vrai GPU ne
touche à rien d'autre.
"""
