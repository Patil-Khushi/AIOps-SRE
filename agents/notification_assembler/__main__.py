"""CLI entry-point for the Notification Assembler (RA-005+006).

Read a ``TriageVerdict`` JSON from stdin, print the resulting
``NotificationAssembly`` JSON on stdout. ``--send`` actually emits the single
combined ``ChatMessage`` (and creates the war-room bridge on Sev-1/Sev-2)
through the chatops seam (default: dry-run / decide only).

Examples::

    cat verdict.json | python -m agents.notification_assembler
    cat verdict.json | python -m agents.notification_assembler --send
"""

from __future__ import annotations

import sys

from agents.alert_triage import TriageVerdict

from .agent import decide, notify


def main() -> None:
    send = "--send" in sys.argv[1:]
    raw = sys.stdin.read()
    if not raw.strip():
        print("RA-005+006: expected TriageVerdict JSON on stdin", file=sys.stderr)
        sys.exit(2)
    verdict = TriageVerdict.model_validate_json(raw)
    if send:
        print(notify(verdict).model_dump_json(indent=2))
    else:
        print(decide(verdict).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
