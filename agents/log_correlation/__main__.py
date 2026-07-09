"""CLI runner for the Log Correlation agent (RA-007).

Examples::

    uv run python -m agents.log_correlation --list
    uv run python -m agents.log_correlation --fixture slow_product_catalog
    uv run python -m agents.log_correlation --fixture slow_product_catalog --provider anthropic

Defaults the LLM provider to ``stub`` so the agent runs without an API key, and
falls back to deterministic synthetic signals when the observability backends
are unreachable — so it demos end-to-end without the cluster up. Override the
LLM with ``--provider anthropic`` once ``ANTHROPIC_API_KEY`` is set in ``.env``.
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
    fixture: str = typer.Option(None, "--fixture", "-f", help="Fixture id to correlate."),
    list_: bool = typer.Option(False, "--list", "-l", help="List available fixtures."),
    provider: str = typer.Option(
        "stub", "--provider", "-p", help="LLM provider: stub | anthropic | openai | ollama."
    ),
    window_minutes: int = typer.Option(
        15,
        "--window-minutes",
        "-w",
        help="Trailing window (minutes, ending now) to correlate over. Live logs "
        "only exist for recent time, so the CLI queries now-W..now by default.",
    ),
    exact_window: bool = typer.Option(
        False,
        "--exact-window",
        help="Use the fixture's literal (historical) window instead of now-W..now. "
        "Offline this still demos via the synthetic fallback; against live Loki it "
        "will find no logs for a past window.",
    ),
) -> None:
    """Run Log Correlation against a golden fixture."""
    os.environ["AIOPS_LLM_PROVIDER"] = provider

    # Defer agent import until after env is set so the LLM gateway picks up the
    # chosen provider on first call.
    from datetime import UTC, datetime, timedelta

    from agents.log_correlation import CorrelationInput
    from agents.log_correlation.agent import correlate, reset_state

    reset_state()

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
    payload = dict(case["input"])
    # Live logs only exist for recent time. Default to a trailing window ending
    # now so a live run against Loki actually finds lines; --exact-window keeps
    # the fixture's historical window (offline synthetic still works either way).
    if not exact_window:
        end = datetime.now(UTC)
        start = end - timedelta(minutes=window_minutes)
        payload["window"] = {"start": start.isoformat(), "end": end.isoformat()}

    rprint(f"[bold]Fixture:[/bold] {fixture}  —  {case.get('description', '')}")
    rprint(f"[bold]LLM provider:[/bold] {provider}")
    rprint(
        f"[bold]Window:[/bold] {'fixture (exact)' if exact_window else f'now-{window_minutes}m..now'}"
    )
    rprint("\n[bold]Input:[/bold]")
    rprint(json.dumps(payload, indent=2))

    result = correlate(CorrelationInput(**payload))

    rprint("\n[bold]Correlation result:[/bold]")
    rprint(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    app()
