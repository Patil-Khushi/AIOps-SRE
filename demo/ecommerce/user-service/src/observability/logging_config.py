"""Structured JSON logging.

Emits one JSON object per line. Two handlers get the same formatter:

- **stdout** — always on. ``kubectl logs`` / ``docker logs`` must keep working.
- **Loki** — on when ``LOKI_URL`` is set. Ships the identical line straight to
  Loki's push API (see ``loki_handler``), so logs reach the backend the same way
  metrics and traces do: direct from the process, no shipper in between.

The spec's key log lines map to levels:
    INFO  "login successful"
    WARN  "invalid credentials"
    ERROR "database connection failed"
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

from .loki_handler import build_loki_handler

_SERVICE = "user-service"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(_SERVICE)
    if logger.handlers:  # already configured
        return logger

    fmt = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "service"},
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # Additive — a Loki that is down, slow or absent costs this logger nothing
    # but the lines it would have shipped. stdout above is unaffected either way.
    loki = build_loki_handler(fmt, _SERVICE)
    if loki is not None:
        logger.addHandler(loki)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


log = configure_logging()
