"""Observability tool providers: Prometheus (metrics) + Jaeger (traces) + Loki (logs) + Grafana (rendering).

Importing this package side-effect-registers the providers with the global
``aiops.tools`` registry via their ``@tool`` decorators. Capabilities exposed:

- ``observability.metrics.query``         (provider ``prometheus``)
- ``observability.metrics.alerts``        (provider ``prometheus``)
- ``observability.metrics.render_panel``  (provider ``grafana``)
- ``observability.traces.services``       (provider ``jaeger``)
- ``observability.traces.search``         (provider ``jaeger``)
- ``observability.logs.query``            (provider ``loki``)
- ``observability.events.query``          (provider ``kubernetes``)

Endpoints default to the local port-forwards (``http://localhost:9090``,
``http://localhost:16686``, ``http://localhost:3100``); override with
``AIOPS_PROMETHEUS_URL`` / ``AIOPS_JAEGER_URL`` / ``AIOPS_LOKI_URL`` /
``AIOPS_GRAFANA_URL``.

``k8s_events`` is the exception to that pattern: it talks to the Kubernetes API
rather than an HTTP endpoint, and is gated off by default behind
``AIOPS_K8S_EVENTS_ENABLED``. It imports the ``kubernetes`` client lazily inside its
function body, so importing this package still works on a ``--extra dev`` install
where that package is absent — see its module docstring.
"""

from __future__ import annotations

from aiops.tools.observability import grafana, jaeger, k8s_events, loki, prometheus

__all__ = ["grafana", "jaeger", "k8s_events", "loki", "prometheus"]
