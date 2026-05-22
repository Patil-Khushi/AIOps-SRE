"""Seed the demo CMDB's owning teams as ServiceNow ``sys_user_group`` rows.

Why this exists: RA-003 sends ``assignment_group=<team name>`` from the
demo CMDB (e.g. ``"Personalization Team"``). ServiceNow's
``assignment_group`` column is a reference to ``sys_user_group``, so a stock
PDI silently drops names it doesn't recognize and the Assignment Group
column in the incident list view shows up empty.

This script reads the team list from ``aiops.tools.itsm._demo_cmdb`` (single
source of truth — same place the CMDB lookup uses), then POSTs each name
to ``/api/now/table/sys_user_group``. Idempotent: groups that already exist
are left alone (no duplicate rows, no field overwrites).

Auth + TLS come from ``.env`` — ``AIOPS_SERVICENOW_INSTANCE_URL``,
``AIOPS_SERVICENOW_USER``, ``AIOPS_SERVICENOW_PASSWORD``, optional
``AIOPS_SERVICENOW_VERIFY_TLS`` (set to ``false`` on a TLS-intercepting
corp proxy; set to a CA bundle path for the proper fix).

Run::

    uv run python scripts/snow_seed_groups.py

Exit codes:
- ``0`` — every group exists in ServiceNow (created or already there)
- ``1`` — missing creds, auth failure, or a row failed to POST
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aiops._dotenv import load_dotenv  # noqa: E402
from aiops.tools.itsm import _demo_cmdb  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

GROUP_DESCRIPTION_TEMPLATE = (
    "Owning team for one or more OpenTelemetry Astronomy Shop services in the "
    "Adaptive AIOps demo CMDB. Seeded by scripts/snow_seed_groups.py — safe to "
    "edit description / members; do not rename (RA-003 routes by exact name)."
)


def _config() -> tuple[str, httpx.BasicAuth, bool | str] | None:
    url = os.environ.get("AIOPS_SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.environ.get("AIOPS_SERVICENOW_USER", "")
    password = os.environ.get("AIOPS_SERVICENOW_PASSWORD", "")
    if not (url and user and password):
        return None
    verify_raw = os.environ.get("AIOPS_SERVICENOW_VERIFY_TLS", "true").strip().lower()
    verify: bool | str
    if verify_raw in {"0", "false", "no"}:
        verify = False
    elif verify_raw in {"1", "true", "yes", ""}:
        verify = True
    else:
        verify = verify_raw
    return url, httpx.BasicAuth(user, password), verify


def _team_names() -> list[str]:
    """Distinct, alphabetized team names from the demo CMDB + the default."""
    names = {row["team"] for row in _demo_cmdb.CMDB_TABLE.values() if row.get("team")}
    default_team = _demo_cmdb.CMDB_DEFAULT.get("team")
    if default_team:
        names.add(default_team)
    return sorted(names)


def _find(client: httpx.Client, base_url: str, name: str) -> str | None:
    """Return sys_id if a group with this exact name exists, else None."""
    res = client.get(
        f"{base_url}/api/now/table/sys_user_group",
        params={
            "sysparm_query": f"name={name}",
            "sysparm_fields": "sys_id,name",
            "sysparm_limit": "1",
        },
    )
    res.raise_for_status()
    rows = (res.json() or {}).get("result") or []
    return rows[0].get("sys_id") if rows else None


def _create(client: httpx.Client, base_url: str, name: str) -> str:
    res = client.post(
        f"{base_url}/api/now/table/sys_user_group",
        json={"name": name, "description": GROUP_DESCRIPTION_TEMPLATE, "active": "true"},
    )
    res.raise_for_status()
    return ((res.json() or {}).get("result") or {}).get("sys_id", "")


def main() -> int:
    cfg = _config()
    if cfg is None:
        print(
            "FAIL: AIOPS_SERVICENOW_INSTANCE_URL / USER / PASSWORD missing from .env",
            file=sys.stderr,
        )
        return 1
    base_url, auth, verify = cfg
    teams = _team_names()
    print(
        f"Seeding {len(teams)} groups into {base_url} as "
        f"{os.environ.get('AIOPS_SERVICENOW_USER')} (verify_tls={verify})"
    )
    created = 0
    skipped = 0
    failed: list[str] = []
    with httpx.Client(
        auth=auth,
        verify=verify,
        timeout=20.0,
        headers={"Accept": "application/json"},
    ) as client:
        for name in teams:
            try:
                existing = _find(client, base_url, name)
                if existing:
                    print(f"  [skip]    {name:<22} (sys_id={existing[:8]}…)")
                    skipped += 1
                    continue
                sys_id = _create(client, base_url, name)
                print(f"  [create]  {name:<22} (sys_id={sys_id[:8]}…)")
                created += 1
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:200]
                print(
                    f"  [FAIL]    {name:<22} HTTP {exc.response.status_code}: {body}",
                    file=sys.stderr,
                )
                failed.append(name)
            except Exception as exc:
                print(f"  [FAIL]    {name:<22} {type(exc).__name__}: {exc}", file=sys.stderr)
                failed.append(name)
    print(f"\nDone. created={created}, skipped={skipped}, failed={len(failed)}")
    if failed:
        print(f"Failed: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
