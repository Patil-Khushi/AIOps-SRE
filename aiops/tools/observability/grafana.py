"""Grafana provider for the ``observability.metrics.render_panel`` capability.

Renders a single dashboard panel as a PNG via Grafana's
``/render/d-solo/<dashboard_uid>?panelId=<id>...`` endpoint and returns
the raw bytes plus its content type.  RA-003 Auto-Ticketing attaches the
PNG to the ServiceNow incident so a triager opens the incident and sees
the actual graph that breached the threshold (DEMO-8 / #60).

Requires the ``grafana-image-renderer`` plugin to be installed on the
target Grafana instance.  The OTel demo's Grafana ships *without* it by
default; enable via the helm values block this PR adds, or set
``GF_INSTALL_PLUGINS=grafana-image-renderer`` on the Grafana pod
directly.  Without the plugin the endpoint returns a 500 — the renderer
captures that as ``ToolResult(ok=False)`` and the auto-ticketing agent
falls through to a no-attachment ticket without raising.

Why bytes (not a URL) in the result: ServiceNow's attachment endpoint
takes a binary body, not a remote URL, so the renderer hands the bytes
straight through.  Callers that need a JSON-safe form base64-encode at
the wire layer.
"""

from __future__ import annotations

import os

import httpx

from aiops.tools.registry import ToolResult, tool

# Default points at the kubectl port-forward in start.ps1
# (Grafana is mounted under /grafana in the OTel demo's frontend-proxy).
# Override for a self-hosted Grafana.
_URL = os.environ.get("AIOPS_GRAFANA_URL", "http://localhost:8080/grafana").rstrip("/")

# Optional API key. The OTel demo's Grafana is unauthenticated on the
# port-forward, so leaving this empty is fine for local dev.  Set it
# (any Editor-or-higher service-account token) when targeting a Grafana
# that enforces auth on /render/*.
_API_KEY = os.environ.get("AIOPS_GRAFANA_API_KEY", "").strip()

# Rendering is heavier than a normal Grafana request: the image-renderer
# spins up headless Chromium per call.  Give it a generous default.
_TIMEOUT = float(os.environ.get("AIOPS_GRAFANA_TIMEOUT", "30"))


@tool(
    name="grafana.observability.metrics.render_panel",
    capability="observability.metrics.render_panel",
    provider="grafana",
    description="Render a Grafana dashboard panel as a PNG via the image-renderer plugin.",
)
def render_panel(
    dashboard_uid: str,
    panel_id: int,
    *,
    from_: str = "now-15m",
    to: str = "now",
    width: int = 800,
    height: int = 400,
    tz: str = "UTC",
) -> ToolResult:
    """GET ``/render/d-solo/{dashboard_uid}?panelId={panel_id}&...``.

    Returns the PNG bytes in ``data["png_bytes"]`` alongside the rendered
    size and the panel coordinates.  On failure (plugin not installed,
    panel not found, Grafana unreachable) returns ``ToolResult(ok=False)``
    so the auto-ticketing agent can log + continue without raising.
    """
    if not dashboard_uid:
        return ToolResult(
            ok=False,
            error="render_panel requires dashboard_uid",
            metadata={"provider": "grafana", "url": _URL},
        )

    params = {
        "panelId": str(panel_id),
        "from": from_,
        "to": to,
        "width": str(width),
        "height": str(height),
        "tz": tz,
    }
    headers = {"Accept": "image/png"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"

    try:
        r = httpx.get(
            f"{_URL}/render/d-solo/{dashboard_uid}",
            params=params,
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolResult(
            ok=False,
            error=f"HTTPError: {exc}",
            metadata={"provider": "grafana", "url": _URL},
        )

    # Defensive: confirm the response really is an image. A Grafana
    # without the image-renderer plugin returns 200 + an HTML error page
    # for some endpoint shapes; ``raise_for_status`` won't catch that.
    content_type = (r.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        return ToolResult(
            ok=False,
            error=(
                "Grafana returned non-image content "
                f"(Content-Type={content_type!r}). "
                "Is the grafana-image-renderer plugin installed?"
            ),
            metadata={"provider": "grafana", "url": _URL},
        )

    return ToolResult(
        ok=True,
        data={
            "png_bytes": r.content,
            "content_type": content_type,
            "dashboard_uid": dashboard_uid,
            "panel_id": panel_id,
            "width": width,
            "height": height,
            "from": from_,
            "to": to,
        },
        metadata={"provider": "grafana", "url": _URL},
    )
