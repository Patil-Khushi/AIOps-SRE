"""CLI runner for the Auto-Healer-lite demo (HITL-1, issue #77).

Two flows:

    # Show that the gate blocks Required actions without an approver:
    uv run python -m agents.auto_healer_lite --deployment product-catalog --no-approve

    # Demo the happy path — a background thread approves after N seconds so
    # the foreground gate-blocked call unblocks and the runbook executes:
    uv run python -m agents.auto_healer_lite --deployment product-catalog \\
        --auto-approve-after 2 --approver demo-cli

The CLI exists for two reasons:

    1. Reviewers can see the gate fire without standing up the FastAPI server
       + Slack integration first.
    2. The eval harness imports the agent at module level; making the CLI a
       separate ``__main__.py`` keeps that import side-effect-free.
"""

from __future__ import annotations

import json
import threading
import time

import typer
from rich import print as rprint

from agents.auto_healer_lite import RestartRecommendation, recommend_restart
from aiops.policy import (
    ApprovalStatus,
    get_approval_registry,
    install_chatops_listener,
    install_default_approver,
)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    deployment: str = typer.Option("product-catalog", "--deployment", "-d"),
    namespace: str = typer.Option("otel-demo", "--namespace", "-n"),
    reason: str = typer.Option(
        "Demo: agent recommends a restart to clear stuck state.", "--reason"
    ),
    timeout: int = typer.Option(
        30, "--timeout", help="Approval window in seconds for this demo run."
    ),
    auto_approve_after: float = typer.Option(
        0,
        "--auto-approve-after",
        help=(
            "If >0, a background thread approves the pending request after this "
            "many seconds.  Use to demo the happy path without wiring Slack."
        ),
    ),
    approver: str = typer.Option(
        "demo-cli", "--approver", help="Approver identity recorded in the audit log."
    ),
    no_approve: bool = typer.Option(
        False,
        "--no-approve",
        help=(
            "Do not approve.  The CLI will wait for ``--timeout`` then show the "
            "EXPIRED outcome — proves the gate physically blocked the action."
        ),
    ),
) -> None:
    install_chatops_listener()
    install_default_approver(timeout_seconds=timeout)

    rec = RestartRecommendation(
        deployment=deployment,
        namespace=namespace,
        reason=reason,
    )

    rprint(f"[bold]Auto-Healer-lite demo[/bold]  target=[cyan]{deployment}[/cyan]")
    rprint(f"  approval window: {timeout}s")
    if no_approve:
        rprint("  mode: [yellow]no-approve[/yellow] — expect EXPIRED")
    elif auto_approve_after > 0:
        rprint(f"  mode: [green]auto-approve after {auto_approve_after}s[/green]")
    else:
        rprint(
            "  mode: [blue]interactive[/blue] — call "
            "POST /api/approvals/<id>/approve from another shell"
        )

    def _auto_approver() -> None:
        time.sleep(auto_approve_after)
        for req in get_approval_registry().list_pending():
            if req.action == "automation.runbook.execute":
                get_approval_registry().decide(
                    req.id, approved=True, approver=approver, reason="auto-approve via CLI"
                )
                rprint(f"  [green]approved[/green] request {req.id}")
                return
        rprint("  [yellow]no pending approval found to auto-approve[/yellow]")

    if auto_approve_after > 0 and not no_approve:
        threading.Thread(target=_auto_approver, daemon=True).start()

    outcome = recommend_restart(rec)

    rprint("\n[bold]Outcome:[/bold]")
    rprint(json.dumps(outcome.model_dump(mode="json"), indent=2))

    if outcome.approval_id:
        try:
            req = get_approval_registry().get(outcome.approval_id)
            rprint("\n[bold]Approval record:[/bold]")
            rprint(json.dumps(req.to_record(), indent=2))
            if req.status is ApprovalStatus.APPROVED:
                rprint("[green]✓ gate allowed the action[/green]")
            else:
                rprint(f"[yellow]✗ gate blocked the action ({req.status.value})[/yellow]")
        except Exception:
            pass


if __name__ == "__main__":
    app()
