"""Publish the runbook library to OneDrive and refresh ``data/runbook_links.json``.

Why this is a script and not part of the alert path
---------------------------------------------------
Runbooks change rarely; incidents happen constantly. Uploading on every
alert would put a fresh copy in OneDrive per notification and hand each
sink a different URL — the channel card and the personal DM would link to
two different files for the same incident. Publishing once and committing
the resulting link map keeps one file per runbook and one link everywhere.

Run it after adding or editing a runbook::

    uv run python -m scripts.publish_runbooks

Prerequisites
-------------
* ``AIOPS_RUNBOOK_PUBLISHER_URL`` — the "AIOps runbook publisher" Power
  Automate flow (Teams webhook trigger -> OneDrive Create file -> Create
  share link). The link it mints is org-scoped and view-only.
* Azure CLI signed in (``az login --allow-no-subscriptions``) so run
  outputs can be read back: the trigger is fire-and-forget, so the share
  link comes from the run history rather than the HTTP response.

Action outputs sit behind short-lived pre-signed URLs, so each run is read
as soon as it completes rather than harvested in a later pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from aiops.runbooks import list_runbooks

_AZ = os.environ.get("AIOPS_AZ_PATH", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd")
_FLOW_API = "https://api.flow.microsoft.com"
_API_VERSION = "2016-11-01"
_LINKS_PATH = Path(os.environ.get("AIOPS_RUNBOOK_LINKS_PATH", "data/runbook_links.json"))

_ENV_ID = os.environ.get("AIOPS_POWER_AUTOMATE_ENV", "")
_FLOW_ID = os.environ.get("AIOPS_RUNBOOK_PUBLISHER_FLOW_ID", "")
_PUBLISHER_URL = os.environ.get("AIOPS_RUNBOOK_PUBLISHER_URL", "")


def _token() -> str:
    out = subprocess.run(
        [
            _AZ,
            "account",
            "get-access-token",
            "--resource",
            "https://service.flow.microsoft.com/",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)["accessToken"]


def _runs(c: httpx.Client, top: int = 1) -> list[dict[str, Any]]:
    r = c.get(
        f"{_FLOW_API}/providers/Microsoft.ProcessSimple/environments/{_ENV_ID}"
        f"/flows/{_FLOW_ID}/runs",
        params={"api-version": _API_VERSION, "$top": top},
    )
    r.raise_for_status()
    return r.json().get("value", [])


def _actions(c: httpx.Client, run_name: str) -> dict[str, Any]:
    r = c.get(
        f"{_FLOW_API}/providers/Microsoft.ProcessSimple/environments/{_ENV_ID}"
        f"/flows/{_FLOW_ID}/runs/{run_name}/actions",
        params={"api-version": _API_VERSION},
    )
    r.raise_for_status()
    return {a["name"]: a for a in r.json().get("value", [])}


def _output_body(action: dict[str, Any] | None) -> dict[str, Any]:
    """Action outputs arrive wrapped in a {statusCode, headers, body} envelope."""
    if not action:
        return {}
    uri = (action.get("properties", {}).get("outputsLink") or {}).get("uri")
    if not uri:
        return {}
    try:
        payload = httpx.get(uri, timeout=60).json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    body = payload.get("body", payload)
    return body if isinstance(body, dict) else {}


def main() -> int:
    missing = [
        n
        for n, v in (
            ("AIOPS_RUNBOOK_PUBLISHER_URL", _PUBLISHER_URL),
            ("AIOPS_POWER_AUTOMATE_ENV", _ENV_ID),
            ("AIOPS_RUNBOOK_PUBLISHER_FLOW_ID", _FLOW_ID),
        )
        if not v
    ]
    if missing:
        print(f"publish_runbooks: set {', '.join(missing)} first", file=sys.stderr)
        return 2

    runbooks = list_runbooks()
    print(f"publishing {len(runbooks)} runbooks")
    links: dict[str, dict[str, str]] = {}

    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    with httpx.Client(headers=headers, timeout=90) as c:
        seen = _runs(c)
        last = seen[0]["name"] if seen else None

        for rb in runbooks:
            resp = httpx.post(
                _PUBLISHER_URL,
                json={"runbook_filename": f"{rb.id}.md", "runbook_markdown": rb.body or ""},
                timeout=60,
            )
            if resp.status_code >= 300:
                print(f"  {rb.id}: trigger failed HTTP {resp.status_code}")
                continue

            run = None
            for _ in range(30):
                time.sleep(2)
                cand = _runs(c)
                if (
                    cand
                    and cand[0]["name"] != last
                    and cand[0]["properties"].get("status") in ("Succeeded", "Failed")
                ):
                    run = cand[0]
                    break
            if run is None:
                print(f"  {rb.id}: no completed run observed")
                continue
            last = run["name"]

            acts = _actions(c, run["name"])
            name = _output_body(acts.get("Create_runbook_file")).get("Name")
            url = _output_body(acts.get("Create_share_link")).get("WebUrl")
            if name and url:
                links[name[:-3] if name.endswith(".md") else name] = {
                    "filename": name,
                    "url": url,
                }
                print(f"  {rb.id}: published")
            else:
                print(
                    f"  {rb.id}: run finished but no link (status={run['properties'].get('status')})"
                )

    if not links:
        print(
            "publish_runbooks: nothing published; leaving the existing map alone", file=sys.stderr
        )
        return 1

    _LINKS_PATH.write_text(json.dumps(links, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(links)} links -> {_LINKS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
