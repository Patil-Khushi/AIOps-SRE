"""Infrastructure half of ``user_service.high_latency`` — network delay via tc.

Provides only inject()/recover(); the Failure object lives in
``user_service/high_latency.py``, which wires these in as its
``inject_infra``/``recover_infra``. Defining a second Failure with the same key
here would be dead code that shadows nothing — see the app-layer module for the
registered definition.
"""
from . import _infra_backend

DELAY_MS = 500


def inject() -> None:
    """Inject 500ms network delay on user-service pods."""
    _infra_backend.inject_network_delay("user-service", delay_ms=DELAY_MS)


def recover() -> None:
    """Remove network delay."""
    _infra_backend.remove_network_delay("user-service")
