"""CLI runner for the Alert Triage agent (RA-001).

Examples::

    uv run python -m agents.alert_triage --list
    uv run python -m agents.alert_triage --fixture payment_cpu_spike
    uv run python -m agents.alert_triage --fixture payment_cpu_spike --provider anthropic

Defaults the LLM provider to ``stub`` so the agent runs without an API key.
Override with ``--provider anthropic`` once ``ANTHROPIC_API_KEY`` is set in ``.env``.
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
    fixture: str = typer.Option(None, "--fixture", "-f", help="Fixture id to triage."),
    list_: bool = typer.Option(False, "--list", "-l", help="List available fixtures."),
    provider: str = typer.Option(
        "stub", "--provider", "-p", help="LLM provider: stub | anthropic | openai | ollama."
    ),
) -> None:
    """Run Alert Triage against a golden fixture."""
    os.environ["AIOPS_LLM_PROVIDER"] = provider

    # Defer agent import until after env is set so the LLM gateway picks up
    # the chosen provider on first call.
    from agents.alert_triage import Alert
    from agents.alert_triage.agent import reset_dedup_store, triage

    fixtures = _load_fixtures()
    cases = {c["id"]: c for c in fixtures.get("cases", [])}

    if list_:
        rprint("[bold]Available fixtures:[/bold]")
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
    rprint("\n[bold]Input alert:[/bold]")
    rprint(json.dumps(case["input"], indent=2))

    # Fresh dedup state per CLI invocation so single-fixture runs are reproducible.
    reset_dedup_store()

    alert = Alert(**case["input"])
    verdict, _ = triage(alert)

    rprint("\n[bold]Triage verdict:[/bold]")
    rprint(json.dumps(verdict.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    app()
