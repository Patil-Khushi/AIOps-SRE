"""Structured JSON logging for the Payment Service.

Two handlers share one formatter: stdout (always on, so ``kubectl logs`` keeps
working) and Loki (on when ``LOKI_URL`` is set — ships the identical line
straight to Loki's push API, no Promtail in between). See ``loki_handler``.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

from .loki_handler import build_loki_handler

_SERVICE = "payment-service"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(_SERVICE)
    if logger.handlers:
        return logger

    fmt = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "service"},
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    loki = build_loki_handler(fmt, _SERVICE)
    if loki is not None:
        logger.addHandler(loki)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


log = configure_logging()
