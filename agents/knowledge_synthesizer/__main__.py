"""CLI: synthesize knowledge from one resolved-incident bundle.

    uv run python -m agents.knowledge_synthesizer < bundle.json
    uv run python -m agents.knowledge_synthesizer bundle.json

Reads a JSON bundle (triage_verdict + rca_verdict + optional context) from a
file argument or stdin and prints the SynthesisResult JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agents.knowledge_synthesizer.agent import run


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    bundle = json.loads(raw)
    print(json.dumps(run(bundle), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
