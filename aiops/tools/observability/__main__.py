"""Smoke CLI for the observability tools.

Examples::

    uv run python -m aiops.tools.observability prom-query 'up'
    uv run python -m aiops.tools.observability prom-alerts
    uv run python -m aiops.tools.observability jaeger-services
    uv run python -m aiops.tools.observability jaeger-search frontend --limit 3
"""

from __future__ import annotations

import json

import typer
from rich import print as rprint

from aiops.tools import (
    get_registry,
    observability,  # noqa: F401 — registers providers
)
from aiops.tools.registry import ToolResult

app = typer.Typer(add_completion=False, help=__doc__)


def _show(label: str, result: ToolResult) -> None:
    rprint(f"[bold]{label}[/bold]")
    if not result.ok:
        rprint(f"[red]error:[/red] {result.error}")
        raise typer.Exit(code=1)
    rprint(json.dumps(result.data, indent=2, default=str))
    if result.metadata:
        rprint(f"[dim]metadata: {result.metadata}[/dim]")


@app.command("prom-query")
def prom_query(promql: str = typer.Argument(..., help="Instant PromQL expression")) -> None:
    """Run an instant PromQL query against Prometheus."""
    res = get_registry().call("observability.metrics.query", promql=promql)
    _show(f"prometheus query: {promql}", res)


@app.command("prom-alerts")
def prom_alerts() -> None:
    """List currently firing / pending Prometheus alerts."""
    res = get_registry().call("observability.metrics.alerts")
    _show("prometheus alerts", res)


@app.command("jaeger-services")
def jaeger_services() -> None:
    """List services known to Jaeger."""
    res = get_registry().call("observability.traces.services")
    _show("jaeger services", res)


@app.command("jaeger-search")
def jaeger_search(
    service: str = typer.Argument(..., help="Service name, e.g. 'frontend'"),
    lookback: str = typer.Option("1h", help="Time window (15m / 1h / 6h / 24h)"),
    limit: int = typer.Option(10, help="Max traces to return"),
) -> None:
    """Search recent traces for a service."""
    res = get_registry().call(
        "observability.traces.search",
        service=service,
        lookback=lookback,
        limit=limit,
    )
    _show(f"jaeger search: {service} (lookback={lookback}, limit={limit})", res)


if __name__ == "__main__":
    app()
