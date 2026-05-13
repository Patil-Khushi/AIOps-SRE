"""CLI runner for the Incident Classifier agent (RA-002).

Examples::

    uv run python -m agents.incident_classifier --list
    uv run python -m agents.incident_classifier --fixture <id>

v0: ``classify`` raises ``NotImplementedError``, so ``--fixture`` will fail
loudly until RA-002 v1 lands. ``--list`` works against the (currently empty)
golden set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich import print as rprint

app = typer.Typer(add_completion=False, help=__doc__)

GOLDEN_PATH = Path(__file__).parent / "evals" / "golden.json"


def _load_fixtures() -> dict:
    with GOLDEN_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


@app.command()
def main(
    fixture: str = typer.Option(None, "--fixture", "-f", help="Fixture id to classify."),
    list_: bool = typer.Option(False, "--list", "-l", help="List available fixtures."),
    provider: str = typer.Option(
        "stub", "--provider", "-p", help="LLM provider: stub | anthropic | openai | ollama."
    ),
) -> None:
    """Run Incident Classifier against a golden fixture."""
    os.environ["AIOPS_LLM_PROVIDER"] = provider

    from agents.incident_classifier import ClassificationInput
    from agents.incident_classifier.agent import classify, reset_for_tests

    reset_for_tests()

    fixtures = _load_fixtures()
    cases = {c["id"]: c for c in fixtures.get("cases", [])}

    if list_:
        rprint("[bold]Available fixtures:[/bold]")
        if not cases:
            rprint("  [yellow](none — golden.json has no cases yet)[/yellow]")
        for cid, c in cases.items():
            rprint(f"  [cyan]{cid:30s}[/cyan]  {c.get('description', '')}")
        return

    if not fixture:
        rprint("[red]Provide --fixture <id> or --list[/red]")
        raise typer.Exit(code=1)

    if fixture not in cases:
        rprint(f"[red]Unknown fixture {fixture!r}[/red]")
        rprint(f"Available: {sorted(cases.keys())}")
        raise typer.Exit(code=1)

    case = cases[fixture]
    rprint(f"[bold]Fixture:[/bold] {fixture}  —  {case.get('description', '')}")
    rprint(f"[bold]LLM provider:[/bold] {provider}")
    rprint("\n[bold]Input:[/bold]")
    rprint(json.dumps(case["input"], indent=2))

    payload = ClassificationInput(**case["input"])
    result = classify(payload)

    rprint("\n[bold]Classification:[/bold]")
    rprint(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    app()
