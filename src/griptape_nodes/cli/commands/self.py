"""Self command for Griptape Nodes CLI."""

import asyncio
import json
import shutil

import typer
from rich.markup import escape
from rich.table import Table

from griptape_nodes.cli.shared import (
    CONFIG_DIR,
    DATA_DIR,
    console,
)
from griptape_nodes.common.diagnostics.report import DiagnosticsReport
from griptape_nodes.retained_mode.events.diagnostics_events import (
    GetDiagnosticsReportRequest,
    GetDiagnosticsReportResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import LoadLibrariesRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils.uv_utils import find_uv_bin
from griptape_nodes.utils.version_utils import (
    get_complete_version_string,
)

config_manager = GriptapeNodes.ConfigManager()
secrets_manager = GriptapeNodes.SecretsManager()
os_manager = GriptapeNodes.OSManager()

app = typer.Typer(help="Manage this CLI installation.")


@app.command()
def uninstall() -> None:
    """Uninstall the CLI."""
    _uninstall_self()


@app.command()
def version() -> None:
    """Print the CLI version."""
    _print_current_version()


@app.command()
def info(
    *,
    show_identity: bool = typer.Option(
        False,
        "--show-identity",
        help="Show real home directory paths and username instead of '~' and '<user>'.",
    ),
) -> None:
    """Display system information for debugging.

    Safe to paste into a bug report: secret values are never shown, credential-shaped
    settings are replaced with '<redacted>', and your home directory and username are
    replaced with '~' and '<user>' unless --show-identity is passed.
    """
    asyncio.run(_print_system_info_async(normalize_identity=not show_identity))


def _print_current_version() -> None:
    """Prints the current version of the script."""
    version_string = get_complete_version_string()
    console.print(f"[bold green]{version_string}[/bold green]")


def _uninstall_self() -> None:
    """Uninstalls itself by removing config/data directories and the executable."""
    console.print("[bold]Uninstalling Griptape Nodes...[/bold]")

    # Remove config and data directories
    console.print("[bold]Removing config and data directories...[/bold]")
    dirs = [(CONFIG_DIR, "Config Dir"), (DATA_DIR, "Data Dir")]
    caveats = []
    for dir_path, dir_name in dirs:
        if dir_path.exists():
            console.print(f"[bold]Removing {dir_name} '{dir_path}'...[/bold]")
            try:
                shutil.rmtree(dir_path)
            except OSError as exc:
                console.print(f"[red]Error removing {dir_name} '{dir_path}': {exc}[/red]")
                caveats.append(
                    f"- [red]Error removing {dir_name} '{dir_path}'. You may want remove this directory manually.[/red]"
                )
        else:
            console.print(f"[yellow]{dir_name} '{dir_path}' does not exist; skipping.[/yellow]")

    # Handle any remaining config files not removed by design
    remaining_config_files = config_manager.config_files
    if remaining_config_files:
        caveats.append("- Some config files were intentionally not removed:")
        caveats.extend(f"\t[yellow]- {file}[/yellow]" for file in remaining_config_files)

    # If there were any caveats to the uninstallation process, print them
    if caveats:
        console.print("[bold]Caveats:[/bold]")
        for line in caveats:
            console.print(line)

    # Remove the executable
    console.print("[bold]Removing the executable...[/bold]")
    console.print("[bold yellow]When done, press Enter to exit.[/bold yellow]")

    # Remove the tool using UV
    uv_path = find_uv_bin()
    os_manager.replace_process([uv_path, "tool", "uninstall", "griptape-nodes"])


async def _print_system_info_async(*, normalize_identity: bool) -> None:
    """Collect a diagnostics report and print it."""
    # Libraries are loaded first so the report can say which ones failed, which is the
    # single most useful thing this command prints.
    await GriptapeNodes.ahandle_request(LoadLibrariesRequest())

    result = await GriptapeNodes.ahandle_request(
        GetDiagnosticsReportRequest(normalize_identity=normalize_identity, broadcast_result=False)
    )
    if not isinstance(result, GetDiagnosticsReportResultSuccess):
        console.print("[red]Attempted to collect system information. Failed to build a diagnostics report.[/red]")
        console.print(f"[red]{escape(str(result.result_details))}[/red]")
        raise typer.Exit(code=1)

    _print_report(result.report)


def _print_report(report: DiagnosticsReport) -> None:
    """Print every section of a diagnostics report."""
    console.print("\n[bold cyan]Griptape Nodes System Information[/bold cyan]\n")

    _print_engine_info(report)
    _print_platform_info(report)
    _print_paths_info(report)
    _print_logs_info(report)
    _print_configuration(report)
    _print_secrets_info(report)
    _print_libraries_info(report)
    _print_projects_info(report)
    _print_warnings(report)
    _print_redaction_notice(report)


def _print_engine_info(report: DiagnosticsReport) -> None:
    """Print engine and interpreter information."""
    engine = report.engine

    console.print("[bold]Engine:[/bold]")
    console.print(f"  Version: {escape(engine.engine_version or 'unknown')}")
    console.print(f"  Install Source: {escape(engine.install_source or 'unknown')}")
    if engine.commit_id:
        console.print(f"  Commit ID: {escape(engine.commit_id)}")
    if engine.engine_name:
        console.print(f"  Engine Name: {escape(engine.engine_name)}")
    if engine.engine_id:
        console.print(f"  Engine ID: {escape(engine.engine_id)}")
    if engine.session_id:
        console.print(f"  Session ID: {escape(engine.session_id)}")
    console.print(f"  Process ID: {engine.process_id}")
    console.print()


def _print_platform_info(report: DiagnosticsReport) -> None:
    """Print information about the machine the engine is running on."""
    host = report.host
    engine = report.engine

    console.print("[bold]Platform:[/bold]")
    console.print(f"  OS: {escape(host.system)}")
    console.print(f"  OS Version: {escape(host.version)}")
    console.print(f"  OS Release: {escape(host.release)}")
    console.print(f"  Architecture: {escape(host.machine)}")
    if host.cpu_count is not None:
        console.print(f"  CPUs: {host.cpu_count}")
    if host.workspace_disk_free_gb is not None and host.workspace_disk_total_gb is not None:
        console.print(f"  Workspace Disk: {host.workspace_disk_free_gb} GB free of {host.workspace_disk_total_gb} GB")
    # `sys.version` wraps onto several lines on some builds; collapse it to one.
    python_version = " ".join(engine.python_version.split())
    console.print(f"  Python Version: {escape(python_version)}")
    console.print(f"  Python Executable: {escape(engine.python_executable)}")
    console.print()


def _print_paths_info(report: DiagnosticsReport) -> None:
    """Print the paths the engine reads and writes, marking the ones that do not exist."""
    paths = report.paths
    missing = set(paths.missing_paths)

    labelled = [
        ("Workspace Directory", paths.workspace_directory),
        ("Config Directory", paths.config_directory),
        ("Config File", paths.user_config_file),
        ("Global Secrets File", paths.global_env_file),
        ("Workspace Secrets File", paths.workspace_env_file),
        ("Libraries Directory", paths.libraries_directory),
        ("Static Files Directory", paths.static_files_directory),
        ("Log Directory", paths.log_directory),
    ]

    console.print("[bold]Paths:[/bold]")
    for label, path in labelled:
        if path is None:
            continue
        suffix = ""
        if path in missing:
            suffix = " [yellow](missing)[/yellow]"
        console.print(f"  {label}: {escape(path)}{suffix}")
    console.print()


def _print_logs_info(report: DiagnosticsReport) -> None:
    """Print how logging is configured and what log history exists."""
    logs = report.logs

    console.print("[bold]Logging:[/bold]")
    console.print(f"  Log Level: {escape(str(logs.log_level))}")
    console.print(f"  Write Log Files: {logs.log_to_file}")
    if logs.retention_days > 0:
        console.print(f"  Keep Log Files For: {logs.retention_days} day(s)")
    else:
        console.print("  Keep Log Files For: forever")
    console.print(f"  Log Files Available: {len(logs.files)}")
    console.print(f"  Lines Captured This Session: {logs.session_lines_captured}")
    console.print()


def _print_configuration(report: DiagnosticsReport) -> None:
    """Print the config files in precedence order, then the merged settings."""
    config = report.config

    console.print("[bold]Configuration Files[/bold] (lowest priority first):")
    for entry in config.files:
        status = "found"
        if not entry.exists:
            status = "not present"
        console.print(f"  {escape(entry.layer)}: {escape(entry.path)} ({status})")
    console.print()

    if config.environment_overrides:
        console.print("[bold]Environment Overrides[/bold] (these beat every config file):")
        for name in config.environment_overrides:
            console.print(f"  {escape(name)}")
        console.print()

    console.print("[bold]Configuration:[/bold]")
    config_json = json.dumps(config.merged, indent=2, default=str)
    # Markup off: the config holds arbitrary strings, and a value containing square
    # brackets would otherwise be eaten as a style tag.
    console.print(config_json, style="dim", markup=False, highlight=False)
    console.print()


def _print_secrets_info(report: DiagnosticsReport) -> None:
    """Print which secrets exist and whether they have a value. Never the values."""
    console.print("[bold]Secrets[/bold] (names and status only, never values):")
    if not report.secrets:
        console.print("  [yellow]No secrets found[/yellow]")
        console.print()
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Secret", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Used From", style="blue")
    table.add_column("Also Found In", style="yellow")

    for secret in report.secrets:
        status = "set"
        if not secret.is_set:
            status = "[yellow]not set[/yellow]"
        # Only the sources after the winning one; the winning one is its own column, and
        # repeating it would make every row look like a shadowing problem.
        shadowed = ", ".join(secret.sources[1:])
        table.add_row(
            escape(secret.name),
            status,
            escape(secret.effective_source or "-"),
            escape(shadowed),
        )

    console.print(table)
    console.print()


def _print_libraries_info(report: DiagnosticsReport) -> None:
    """Print every library the engine tried to load, then the problems it hit."""
    console.print("[bold]Libraries:[/bold]")
    if not report.libraries:
        console.print("  [yellow]No libraries registered[/yellow]")
        console.print()
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Library Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Health", style="yellow")
    table.add_column("Loaded To", style="blue")

    for library in report.libraries:
        name = escape(library.name)
        if not library.enabled:
            name = f"{name} [dim](disabled)[/dim]"
        table.add_row(
            name,
            escape(library.version or "unknown"),
            escape(str(library.fitness)),
            escape(str(library.lifecycle_state)),
        )

    console.print(table)
    console.print()

    libraries_with_problems = [library for library in report.libraries if library.problems]
    if not libraries_with_problems:
        return

    console.print("[bold]Library Problems:[/bold]")
    for library in libraries_with_problems:
        console.print(f"  [bold]{escape(library.name)}[/bold]")
        console.print(str(library.problems), style="yellow", markup=False, highlight=False)
    console.print()


def _print_projects_info(report: DiagnosticsReport) -> None:
    """Print the project templates the engine loaded, and any problems found in them."""
    if not report.projects:
        return

    console.print("[bold]Projects:[/bold]")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Project", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Active", style="blue")
    table.add_column("Problems", style="yellow")

    for project in report.projects:
        status = str(project.validation_status)
        if not project.loaded:
            status = f"{status} (failed to load)"
        active = ""
        if project.is_current:
            active = "yes"
        problem_count = ""
        if project.problems:
            problem_count = str(len(project.problems))
        table.add_row(escape(project.name or project.project_id), escape(status), active, problem_count)

    console.print(table)
    console.print()

    projects_with_problems = [project for project in report.projects if project.problems]
    if not projects_with_problems:
        return

    console.print("[bold]Project Problems:[/bold]")
    for project in projects_with_problems:
        console.print(f"  [bold]{escape(project.name or project.project_id)}[/bold]")
        for problem in project.problems:
            location = escape(problem.field_path)
            if problem.line_number is not None:
                location = f"{location}:{problem.line_number}"
            console.print(
                f"    {escape(problem.severity)}: {location}: {escape(problem.message)}",
                style="yellow",
            )
    console.print()


def _print_warnings(report: DiagnosticsReport) -> None:
    """Print anything that could not be collected, so no section reads as empty by accident."""
    if not report.collection_warnings:
        return

    console.print("[bold yellow]Could Not Be Collected:[/bold yellow]")
    for warning in report.collection_warnings:
        console.print(f"  [yellow]{escape(warning)}[/yellow]")
    console.print()


def _print_redaction_notice(report: DiagnosticsReport) -> None:
    """State what was removed, so a hidden value is never mistaken for an absent one."""
    redaction = report.redaction
    if redaction.total == 0:
        console.print("[dim]No values were hidden from this output.[/dim]")
        console.print()
        return

    reasons = ", ".join(f"{reason}: {count}" for reason, count in redaction.counts.items())
    console.print(
        f"[dim]{redaction.total} value(s) were hidden from this output and shown as "
        f"'<redacted>' ({escape(reasons)}).[/dim]"
    )
    console.print()
