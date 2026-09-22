"""Diagnostics command for Griptape Nodes CLI."""

import asyncio
from pathlib import Path

import typer
from rich.markup import escape

from griptape_nodes.cli.shared import console
from griptape_nodes.retained_mode.events.diagnostics_events import (
    CollectDiagnosticsRequest,
    CollectDiagnosticsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import LoadLibrariesRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

app = typer.Typer(help="Collect troubleshooting information about this installation.")


@app.callback()
def diagnostics() -> None:
    """Collect troubleshooting information about this installation.

    Present so the commands below keep their names. Typer folds a single-command app
    into the parent command, which would make `gtn diagnostics collect` unavailable.
    """


@app.command()
def collect(
    output: Path = typer.Option(  # noqa: B008
        Path(),
        "--output",
        "-o",
        help="Where to write the bundle. A directory gets a generated file name.",
    ),
    *,
    skip_logs: bool = typer.Option(
        False,
        "--skip-logs",
        help="Leave the engine's log files out of the bundle.",
    ),
    skip_libraries: bool = typer.Option(
        False,
        "--skip-libraries",
        help="Do not load libraries first. Faster, but the bundle cannot say which ones fail to load.",
    ),
    show_identity: bool = typer.Option(
        False,
        "--show-identity",
        help="Keep real home directory paths and username instead of '~' and '<user>'.",
    ),
) -> None:
    """Collect logs and setup into one zip to attach to a bug report.

    Secret values the engine holds are removed, credential-shaped settings are replaced
    with '<redacted>', and your home directory and username are replaced with '~' and
    '<user>' unless --show-identity is passed. The bundle's manifest.json counts
    everything that was removed. Read the logs and the workflow file before sharing it:
    a secret the engine was never told about, in a shape nothing matches, is not
    something it can find.
    """
    asyncio.run(
        _collect_async(
            output,
            include_logs=not skip_logs,
            load_libraries=not skip_libraries,
            normalize_identity=not show_identity,
        )
    )


async def _collect_async(output: Path, *, include_logs: bool, load_libraries: bool, normalize_identity: bool) -> None:
    """Collect a diagnostics bundle and report where it landed.

    Both requests below are guarded, and guarded broadly, because either one can be the call
    that starts the engine -- and a broken engine is the usual reason somebody is collecting
    a bundle to attach to a bug report. A node library that raises on import, a config file
    the engine cannot get past: left unguarded, the command that exists to package that
    answer instead hands back a traceback, which is what the user already had.
    """
    if load_libraries:
        # Libraries are loaded so the bundle can say which ones failed, which is usually
        # the answer. Skipping it leaves the libraries section empty, not wrong.
        try:
            with console.status("Loading libraries..."):
                await GriptapeNodes.ahandle_request(LoadLibrariesRequest())
        except Exception as err:
            # Not fatal. The bundle records which libraries are missing, and the rest of it
            # still describes the machine somebody is asking about.
            console.print(f"[yellow]Attempted to load the libraries. Failed: {escape(str(err))}[/yellow]")
            console.print("[yellow]The bundle is collected anyway, without them.[/yellow]")

    try:
        with console.status("Collecting diagnostics..."):
            result = await GriptapeNodes.ahandle_request(
                CollectDiagnosticsRequest(
                    include_logs=include_logs,
                    # Nothing is open in a CLI-launched engine, so asking for the current
                    # workflow could only ever add a warning saying there wasn't one.
                    include_current_workflow=False,
                    normalize_identity=normalize_identity,
                    output_path=str(output),
                    broadcast_result=False,
                )
            )
    except Exception as err:
        console.print("[red]Attempted to collect a diagnostics bundle. The engine itself could not be started.[/red]")
        console.print(f"[red]{escape(str(err))}[/red]")
        raise typer.Exit(code=1) from err

    if not isinstance(result, CollectDiagnosticsResultSuccess):
        console.print("[red]Attempted to collect a diagnostics bundle. Failed to write it.[/red]")
        console.print(f"[red]{escape(str(result.result_details))}[/red]")
        raise typer.Exit(code=1)

    manifest = result.manifest

    console.print()
    console.print("[bold green]Diagnostics bundle written to:[/bold green]")
    # Soft wrap so the path is never broken across lines: it is about to be copied.
    console.print(f"  {escape(str(result.path))}", soft_wrap=True)
    console.print(f"  Size: {result.size_bytes / 1024:.1f} KB")
    console.print(f"  Files: {len(manifest.entries)}")
    console.print(f"  Values hidden: {manifest.redaction.total}")
    console.print()

    for entry in manifest.entries:
        console.print(f"  {escape(entry.path)}", style="dim")
    console.print()

    if manifest.warnings:
        console.print("[bold yellow]Left out of this bundle:[/bold yellow]")
        for warning in manifest.warnings:
            console.print(f"  [yellow]{escape(warning)}[/yellow]")
        console.print()

    # The line printed on every collect, which is where a promise about secrets would
    # actually be read. It names the two files free text ends up in rather than declaring
    # the bundle clean: the engine removed what it knows, and what it knows is not
    # everything a machine might be holding.
    console.print("[dim]Read manifest.json to see what was removed.[/dim]")
    console.print("[dim]Skim the logs and the workflow before posting this anywhere public.[/dim]")
