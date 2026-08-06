"""Grafana provider for the ``observability.metrics.render_panel`` capability.

Renders a single dashboard panel as a PNG via Grafana's
``/render/d-solo/<dashboard_uid>?panelId=<id>...`` endpoint and returns
the raw bytes plus its content type.  RA-003 Auto-Ticketing attaches the
PNG to the ServiceNow incident so a triager opens the incident and sees
the actual graph that breached the threshold (DEMO-8 / #60).

Requires server-side rendering to be enabled on the target Grafana.  The
OTel demo's Grafana ships *without* it by default; enable it via the
``imageRenderer`` block in ``infra/observability/grafana-values.yaml`` (DEMO-8
/ #60), which deploys the ``grafana-image-renderer`` as a sidecar pod, or
set ``GF_INSTALL_PLUGINS=grafana-image-renderer`` on the Grafana pod
directly.  Without a renderer the endpoint returns a 500 — the renderer
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

# Default URL points at the kubectl port-forward in start.ps1 (Grafana is
# mounted under /grafana in the OTel demo's frontend-proxy). If you
# port-forward Grafana directly (typically on :3000) you need to set
# AIOPS_GRAFANA_URL to "http://localhost:3000" — the default path-suffix
# won't apply.


def _config() -> tuple[str, str, float]:
    """Return (base_url, api_key, timeout). Read lazily on every call so
    ``.env`` edits take effect on the next request without re-importing
    the module (mirrors ``aiops.tools.itsm.servicenow._config`` — see #144
    review). ``api_key`` is ``""`` when unset; the OTel demo's local
    Grafana is unauthenticated so that's the common path."""
    url = os.environ.get("AIOPS_GRAFANA_URL", "http://localhost:8080/grafana").rstrip("/")
    api_key = os.environ.get("AIOPS_GRAFANA_API_KEY", "").strip()
    # Rendering is heavier than a normal Grafana request: the image-renderer
    # spins up headless Chromium per call. Give it a generous default.
    timeout = float(os.environ.get("AIOPS_GRAFANA_TIMEOUT", "30"))
    return url, api_key, timeout


@tool(
    name="grafana.observability.metrics.render_panel",
    capability="observability.metrics.render_panel",
    provider="grafana",
    description="Render a Grafana panel (or a whole dashboard in kiosk mode) as a PNG via the image-renderer plugin.",
)
def render_panel(
    dashboard_uid: str,
    panel_id: int | None = None,
    *,
    time_range: str | None = None,
    from_: str = "now-15m",
    to: str = "now",
    width: int = 800,
    height: int = 400,
    tz: str = "UTC",
    format: str = "png",
) -> ToolResult:
    """Render a Grafana view as a PNG. Two modes:

    - ``panel_id`` set -> single panel via ``/render/d-solo/{uid}?panelId=``
      (already chrome-less).
    - ``panel_id=None`` -> the WHOLE dashboard via ``/render/d/{uid}`` in
      **kiosk** mode (no nav bar / news popups). Collapsed rows stay
      collapsed, so a dashboard whose only expanded row is a summary (e.g. the
      OTel Collector "Overview") renders as just that summary + thin collapsed
      bars. Give it a taller ``height`` (e.g. 860) than a single panel.

    Returns the PNG bytes in ``data["png_bytes"]`` alongside the rendered
    size and the panel coordinates.  On failure (plugin not installed,
    panel not found, Grafana unreachable) returns ``ToolResult(ok=False)``
    so the auto-ticketing agent can log + continue without raising.

    Contract note: ``data["png_bytes"]`` is raw ``bytes``. Do not pass the
    ``data`` dict to ``json.dumps`` or anything that serializes it without
    base64-encoding the value first — bytes are not JSON-serializable and
    will raise ``TypeError``. The current consumer (auto-ticketing) hands
    the bytes straight to ServiceNow's binary attachment endpoint.
    """
    url, api_key, timeout = _config()

    if not dashboard_uid:
        return ToolResult(
            ok=False,
            error="render_panel requires dashboard_uid",
            metadata={"provider": "grafana", "url": url},
        )

    # PNG is the only output the image-renderer produces; accept the kwarg
    # (RA-003 passes format="png" per #196) but reject anything else loudly
    # rather than silently returning a PNG under the wrong extension.
    if format != "png":
        return ToolResult(
            ok=False,
            error=f"unsupported format {format!r}; the image-renderer only emits PNG",
            metadata={"provider": "grafana", "url": url},
        )

    # A single ``time_range`` (e.g. "15m", "6h") is the panel-config surface
    # (#196); expand it to Grafana's relative from/to window. Explicit
    # ``from_``/``to`` still work for callers that need a custom window.
    if time_range:
        from_ = f"now-{time_range}"
        to = "now"

    params = {
        "from": from_,
        "to": to,
        "width": str(width),
        "height": str(height),
        "tz": tz,
    }
    if panel_id is None:
        # Whole-dashboard render. kiosk mode strips the nav bar + the "what's
        # new" popups that otherwise overlay a full-page render.
        endpoint = f"{url}/render/d/{dashboard_uid}"
        params["kiosk"] = "true"
    else:
        endpoint = f"{url}/render/d-solo/{dashboard_uid}"
        params["panelId"] = str(panel_id)
    headers = {"Accept": "image/png"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        r = httpx.get(
            endpoint,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolResult(
            ok=False,
            error=f"HTTPError: {exc}",
            metadata={"provider": "grafana", "url": url},
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
            metadata={"provider": "grafana", "url": url},
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
            "time_range": time_range,
            "from": from_,
            "to": to,
            "format": format,
        },
        metadata={"provider": "grafana", "url": url},
    )
