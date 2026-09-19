"""Journalisation — une ligne par événement, horodatée en UTC.

UTC et pas l'heure locale : une boîte extérieure qu'on va déboguer au
téléphone doit produire des lignes comparables à celles du serveur, sans se
demander quel fuseau a été appliqué.
"""

from __future__ import annotations

import logging
import sys
import time


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = self.converter(record.created)
        return time.strftime(datefmt or "%Y-%m-%dT%H:%M:%S", ct) + "Z"


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        UTCFormatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # uvicorn pose ses propres handlers avec son propre format : on les retire
    # et on laisse remonter vers le root, sinon le journal est en deux styles.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
