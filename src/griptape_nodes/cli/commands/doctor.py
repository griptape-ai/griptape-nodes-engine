"""Doctor command for Griptape Nodes CLI."""

from __future__ import annotations

import asyncio

import typer
from rich.box import HEAVY_EDGE
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from griptape_nodes.cli.shared import console
from griptape_nodes.common.diagnostics.health import HealthReport, HealthStatus
from griptape_nodes.retained_mode.events.diagnostics_events import (
    RunHealthChecksRequest,
    RunHealthChecksResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import LoadLibrariesRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

_STATUS_LABELS: dict[HealthStatus, str] = {
    HealthStatus.PASS: "[bold green]PASS[/bold green]",
    HealthStatus.WARN: "[bold yellow]WARN[/bold yellow]",
    HealthStatus.FAIL: "[bold red]FAIL[/bold red]",
}


def doctor_command() -> None:
    """Run health checks on the Griptape Nodes engine."""
    asyncio.run(_doctor_async())


async def _doctor_async() -> None:
    """Run the engine's health checks and print what they found."""
    # Libraries are loaded first because "which libraries are broken" is one of the
    # checks, and an engine that has not loaded any would report none at all.
    with console.status("Loading libraries..."):
        await GriptapeNodes.ahandle_request(LoadLibrariesRequest())

    with console.status("Running health checks..."):
        result = await GriptapeNodes.ahandle_request(RunHealthChecksRequest(broadcast_result=False))

    if not isinstance(result, RunHealthChecksResultSuccess):
        console.print("[red]Attempted to run health checks. Failed before any check could run.[/red]")
        console.print(f"[red]{escape(str(result.result_details))}[/red]")
        raise typer.Exit(code=1)

    _print_health_report(result.health)

    # Only a failure is worth a nonzero exit: a warning is something to fix eventually,
    # and scripts calling this should not break over one.
    if result.health.status is HealthStatus.FAIL:
        raise typer.Exit(code=1)


def _print_health_report(health: HealthReport) -> None:
    """Print one row per check, then the remedy for anything that was not a pass."""
    table = Table(show_header=True, box=HEAVY_EDGE, show_lines=True, expand=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    for check in health.results:
        table.add_row(escape(check.name), _STATUS_LABELS[check.status], escape(check.summary))

    console.print(table)

    remedies = [check for check in health.results if check.remedy is not None]
    if not remedies:
        console.print("[bold green]Everything checks out.[/bold green]")
        return

    console.print()
    console.print("[bold]What to do:[/bold]")
    for check in remedies:
        # Padded rather than prefixed with spaces, so a remedy long enough to wrap stays
        # indented under its own check name instead of starting again at the left margin.
        console.print(Padding(Text(f"{check.name}: {check.remedy}"), (0, 0, 0, 2)))
