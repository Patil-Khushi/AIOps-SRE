"""Observability tool providers: Prometheus (metrics) + Jaeger (traces) + Loki (logs) + Grafana (rendering).

Importing this package side-effect-registers the providers with the global
``aiops.tools`` registry via their ``@tool`` decorators. Capabilities exposed:

- ``observability.metrics.query``         (provider ``prometheus``)
- ``observability.metrics.alerts``        (provider ``prometheus``)
- ``observability.metrics.render_panel``  (provider ``grafana``)
- ``observability.traces.services``       (provider ``jaeger``)
- ``observability.traces.search``         (provider ``jaeger``)
- ``observability.logs.query``            (provider ``loki``)

Endpoints default to the local port-forwards (``http://localhost:9090``,
``http://localhost:16686``, ``http://localhost:3100``); override with
``AIOPS_PROMETHEUS_URL`` / ``AIOPS_JAEGER_URL`` / ``AIOPS_LOKI_URL`` /
``AIOPS_GRAFANA_URL``.
"""

from __future__ import annotations

from aiops.tools.observability import grafana, jaeger, loki, prometheus

__all__ = ["grafana", "jaeger", "loki", "prometheus"]
