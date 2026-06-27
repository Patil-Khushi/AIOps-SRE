"""CLI entry-point for RA-006 (standalone).

Read a ``TriageVerdict`` JSON from stdin, print the ``WarRoomAssembly`` JSON.
``--assemble`` creates the war room through the chatops seam (default: decide).

    cat verdict.json | python -m agents.war_room_assembler
    cat verdict.json | python -m agents.war_room_assembler --assemble
"""

from __future__ import annotations

import sys

from agents.alert_triage import TriageVerdict

from .agent import assemble, decide


def main() -> None:
    do_assemble = "--assemble" in sys.argv[1:]
    raw = sys.stdin.read()
    if not raw.strip():
        print("RA-006: expected TriageVerdict JSON on stdin", file=sys.stderr)
        sys.exit(2)
    verdict = TriageVerdict.model_validate_json(raw)
    if do_assemble:
        print(assemble(verdict).model_dump_json(indent=2))
    else:
        print(decide(verdict).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
