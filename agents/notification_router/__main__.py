"""CLI entry-point for RA-005 (standalone).

Read a ``TriageVerdict`` JSON from stdin, print the ``RoutingDecision`` JSON.
``--send`` emits the routing notification through the chatops seam.

    cat verdict.json | python -m agents.notification_router
    cat verdict.json | python -m agents.notification_router --send
"""

from __future__ import annotations

import sys

from agents.alert_triage import TriageVerdict

from .agent import decide, route


def main() -> None:
    send = "--send" in sys.argv[1:]
    raw = sys.stdin.read()
    if not raw.strip():
        print("RA-005: expected TriageVerdict JSON on stdin", file=sys.stderr)
        sys.exit(2)
    verdict = TriageVerdict.model_validate_json(raw)
    if send:
        print(route(verdict).model_dump_json(indent=2))
    else:
        print(decide(verdict).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
