"""Structured JSON logging for the Payment Service."""
import logging
import sys

from pythonjsonlogger import jsonlogger

_SERVICE = "payment-service"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(_SERVICE)
    if logger.handlers:
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