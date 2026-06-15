"""CLI runner for the Runbook Executor (RA-004) demo.

Two flows, mirroring auto_healer_lite so reviewers can see the gate fire without
standing up FastAPI + Slack:

    # Gate blocks the destructive step (no approver) — expect status "denied":
    uv run python -m agents.runbook_executor --service payment --tags crash,oom --no-approve

    # Happy path — a background thread approves so the destructive step runs:
    uv run python -m agents.runbook_executor --service payment --tags crash,oom \\
        --auto-approve-after 1 --approver demo-cli
"""

from __future__ import annotations

import json
import threading
import time

import typer
from rich import print as rprint

from agents.runbook_executor import Incident, execute_runbook
from aiops.policy import (
    ApprovalRequester,
    get_approval_registry,
    get_gate,
)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    service: str = typer.Option("payment", "--service", "-s"),
    severity: str = typer.Option("sev1", "--severity"),
    tags: str = typer.Option("crash,oom", "--tags", help="Comma-separated symptom tags."),
    timeout: int = typer.Option(30, "--timeout", help="Approval window (seconds)."),
    auto_approve_after: float = typer.Option(
        0, "--auto-approve-after", help="If >0, approve pending requests after N seconds."
    ),
    approver: str = typer.Option("demo-cli", "--approver"),
    no_approve: bool = typer.Option(False, "--no-approve", help="Never approve; expect denied."),
) -> None:
    reg = get_approval_registry()
    if not no_approve:
        get_gate().set_approver(ApprovalRequester(reg, timeout_seconds=timeout))

    incident = Incident(
        service=service, severity=severity, tags=[t.strip() for t in tags.split(",") if t.strip()]
    )
    rprint(f"[bold]Runbook Executor (RA-004)[/bold]  service=[cyan]{service}[/cyan] sev={severity}")

    def _auto_approver() -> None:
        time.sleep(auto_approve_after)
        for _ in range(200):
            pending = reg.list_pending()
            if pending:
                reg.decide(pending[0].id, approved=True, approver=approver, reason="auto-approve")
            time.sleep(0.05)

    if auto_approve_after > 0 and not no_approve:
        threading.Thread(target=_auto_approver, daemon=True).start()

    execution = execute_runbook(incident)
    rprint("\n[bold]Execution:[/bold]")
    rprint(json.dumps(execution.model_dump(mode="json"), indent=2))
    color = {"resolved": "green", "rolled_back": "yellow", "denied": "yellow"}.get(
        execution.status, "red"
    )
    rprint(f"[{color}]status = {execution.status}[/{color}]  ({execution.reason})")


if __name__ == "__main__":
    app()
