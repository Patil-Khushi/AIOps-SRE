"""Structured JSON logging.

Emits one JSON object per line so Loki/Promtail can parse fields directly.
The spec's key log lines map to levels:
    INFO  "login successful"
    WARN  "invalid credentials"
    ERROR "database connection failed"
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

_SERVICE = "user-service"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(_SERVICE)
    if logger.handlers:  # already configured
        return logger

    handler = logging.StreamHandler(sys.stdout)
    fmt = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "service"},
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


log = configure_logging()
