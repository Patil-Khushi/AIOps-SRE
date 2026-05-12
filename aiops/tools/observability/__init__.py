"""Observability tool providers: Prometheus (metrics) + Jaeger (traces).

Importing this package side-effect-registers the providers with the global
``aiops.tools`` registry via their ``@tool`` decorators. Capabilities exposed:

- ``observability.metrics.query``       (provider ``prometheus``)
- ``observability.metrics.alerts``      (provider ``prometheus``)
- ``observability.traces.services``     (provider ``jaeger``)
- ``observability.traces.search``       (provider ``jaeger``)

Endpoints default to the local port-forwards (``http://localhost:9090`` and
``http://localhost:16686``); override with ``AIOPS_PROMETHEUS_URL`` /
``AIOPS_JAEGER_URL``.
"""

from __future__ import annotations

from aiops.tools.observability import jaeger, prometheus  # noqa: F401

__all__ = ["jaeger", "prometheus"]
