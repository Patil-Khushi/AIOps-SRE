"""The three services' loki_handler.py copies must stay byte-identical.

user-service, order-service and payment-service are separately built container
images with no shared package between them, so the direct-to-Loki shipper is
vendored into each one rather than imported. That is a deliberate trade — adding
a shared library just to ship logs would couple three independently deployable
images — but it means a fix applied to one copy and not the others is invisible
until the un-fixed service silently loses log lines during an incident.

tests/test_loki_handler.py exercises the user-service copy. This test is what
makes that sufficient coverage for all three.
"""

import hashlib
from pathlib import Path

import pytest

_ECOMMERCE = Path(__file__).resolve().parents[1] / "demo" / "ecommerce"
_SERVICES = ("user-service", "order-service", "payment-service")
_REFERENCE = "user-service"  # the copy test_loki_handler.py imports


def _handler_path(service: str) -> Path:
    return _ECOMMERCE / service / "src" / "observability" / "loki_handler.py"


def test_every_service_has_a_loki_handler():
    missing = [s for s in _SERVICES if not _handler_path(s).is_file()]
    assert not missing, f"loki_handler.py missing for: {', '.join(missing)}"


@pytest.mark.parametrize("service", [s for s in _SERVICES if s != _REFERENCE])
def test_handler_is_byte_identical_to_reference(service):
    reference = _handler_path(_REFERENCE).read_bytes()
    candidate = _handler_path(service).read_bytes()

    if candidate == reference:
        return

    # Normalising line endings first so a stray CRLF checkout reports as the
    # whitespace problem it is rather than as a content divergence.
    if candidate.replace(b"\r\n", b"\n") == reference.replace(b"\r\n", b"\n"):
        pytest.fail(
            f"{service}/src/observability/loki_handler.py differs from "
            f"{_REFERENCE} only in line endings — check .gitattributes / core.autocrlf"
        )

    pytest.fail(
        f"{service}/src/observability/loki_handler.py has diverged from {_REFERENCE} "
        f"(sha256 {hashlib.sha256(candidate).hexdigest()[:12]} vs "
        f"{hashlib.sha256(reference).hexdigest()[:12]}). Apply the change to all "
        f"{len(_SERVICES)} copies: {', '.join(_SERVICES)}"
    )
