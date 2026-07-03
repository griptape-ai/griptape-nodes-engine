"""Models command for managing AI models."""

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING

import typer
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
from rich.table import Table

from griptape_nodes.cli.shared import console
from griptape_nodes.retained_mode.events.model_events import (
    DeleteModelDownloadRequest,
    DeleteModelDownloadResultFailure,
    DeleteModelDownloadResultSuccess,
    DeleteModelRequest,
    DeleteModelResultFailure,
    DeleteModelResultSuccess,
    ListModelDownloadsRequest,
    ListModelDownloadsResultFailure,
    ListModelDownloadsResultSuccess,
    ListModelsRequest,
    ListModelsResultFailure,
    ListModelsResultSuccess,
    ModelInfo,
    QueryInfo,
    SearchModelsRequest,
    SearchModelsResultFailure,
    SearchModelsResultSuccess,
)
from griptape_nodes.retained_mode.managers.model_manager import _PROGRESS_PIPE_ENV_VAR
from griptape_nodes.retained_mode.retained_mode import GriptapeNodes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.model_events import ModelDownloadStatus

app = typer.Typer(help="Manage AI models.")
downloads_app = typer.Typer(help="Manage model download tracking records.")

# Add downloads subcommand
app.add_typer(downloads_app, name="downloads")


@app.command("download")
def download_command(
    model_id: str = typer.Argument(..., help="Model ID or URL (e.g., 'microsoft/DialoGPT-medium')"),
    local_dir: str | None = typer.Option(None, "--local-dir", help="Local directory to download the model to"),
    revision: str = typer.Option("main", "--revision", help="Git revision to download"),
) -> None:
    """Download a model from Hugging Face Hub."""
    asyncio.run(_download_model(model_id, local_dir, revision))


@app.command("list")
def list_command() -> None:
    """List all downloaded model files in local cache."""
    asyncio.run(_list_models())


@app.command("delete")
def delete_command(
    model_id: str = typer.Argument(..., help="Model ID to delete (e.g., 'microsoft/DialoGPT-medium')"),
) -> None:
    """Delete model files from local cache."""
    asyncio.run(_delete_model(model_id))


@downloads_app.command("status")
def downloads_status_command(
    model_id: str = typer.Argument(None, help="Optional model ID to check download status for"),
) -> None:
    """Show download status for a specific model or all models."""
    asyncio.run(_get_model_status(model_id))


@downloads_app.command("list")
def downloads_list_command() -> None:
    """List all model download status records."""
    asyncio.run(_get_model_status(None))


@downloads_app.command("delete")
def downloads_delete_command(
    model_id: str = typer.Argument(
        ..., help="Model ID to delete download status for (e.g., 'microsoft/DialoGPT-medium')"
    ),
) -> None:
    """Delete download status tracking records for a model."""
    asyncio.run(_delete_model_status(model_id))


@app.command("search")
def search_command(
    query: str | None = typer.Argument(None, help="Search query to match against model names"),
    task: str | None = typer.Option(None, "--task", help="Filter by task type"),
    limit: int = typer.Option(20, "--limit", help="Maximum number of results (max: 100)"),
    sort: str = typer.Option("downloads", "--sort", help="Sort results by"),
    direction: str = typer.Option("desc", "--direction", help="Sort direction"),
) -> None:
    """Search for models on Hugging Face Hub."""
    asyncio.run(_search_models(query, task, limit, sort, direction))


def _emit_error_event(error_type: str, error_message: str) -> None:
    """Write a structured JSON error event to stderr when running in pipe mode.

    Only emits when GRIPTAPE_NODES_PROGRESS_PIPE is set, i.e. when spawned by
    the ModelManager subprocess. On direct CLI invocations this is a no-op.
    Uses stderr to avoid mixing with stdout JSON progress events.
    """
    if os.environ.get(_PROGRESS_PIPE_ENV_VAR) == "1":
        sys.stderr.write(json.dumps({"error_type": error_type, "error_message": error_message}) + "\n")
        sys.stderr.flush()


async def _download_model(
    model_id: str,
    local_dir: str | None,
    revision: str,
) -> None:
    """Download a model from Hugging Face Hub.

    Args:
        model_id: Model ID or URL to download
        local_dir: Local directory to download the model to
        revision: Git revision to download
    """
    console.print(f"[bold green]Downloading model: {model_id}[/bold green]")

    try:
        # ModelManager DownloadModelRequest will use this command so it's important that we don't use the request ourselves
        model_manager = GriptapeNodes.ModelManager()
        local_path = model_manager.download_model(
            model_id=model_id,
            local_dir=local_dir,
            revision=revision,
            allow_patterns=None,
            ignore_patterns=None,
        )

        # Success case
        console.print("[bold green]Model downloaded successfully![/bold green]")
        console.print(f"[green]Downloaded to: {local_path}[/green]")

    except GatedRepoError as e:
        _emit_error_event("gated_repo", str(e))
        console.print(f"[bold red]Model download failed:[/bold red] {e}")
        sys.exit(1)

    except RepositoryNotFoundError as e:
        _emit_error_event("repo_not_found", str(e))
        console.print(f"[bold red]Model download failed:[/bold red] {e}")
        sys.exit(1)

    except Exception as e:
        console.print("[bold red]Model download failed:[/bold red]")
        console.print(f"[red]{e}[/red]")
        sys.exit(1)


async def _list_models() -> None:
    """List all downloaded models in the local cache."""
    console.print("[bold green]Listing cached models...[/bold green]")

    # Create the list request
    request = ListModelsRequest()

    try:
        # Use the ModelManager to handle the listing
        result = await GriptapeNodes.ahandle_request(request)
        if isinstance(result, ListModelsResultSuccess):
            # Success case
            models = result.models
            if models:
                console.print(f"[bold green]Found {len(models)} cached models:[/bold green]")

                table = Table()
                table.add_column("Model ID", style="green")
                table.add_column("Size (GB)", style="cyan", justify="right")

                for model in models:
                    size_gb = round((model.size_bytes or 0) / (1024**3), 2) if model.size_bytes else 0.0
                    table.add_row(model.model_id, str(size_gb))
                console.print(table)
            else:
                console.print("[yellow]No models found in local cache[/yellow]")

        # Failure case

        elif isinstance(result, ListModelsResultFailure):
            console.print("[bold red]Model listing failed:[/bold red]")
            if result.result_details:
                console.print(f"[red]{result.result_details}[/red]")
            if result.exception:
                console.print(f"[dim]Error: {result.exception}[/dim]")
        else:
            console.print("[bold red]Model listing failed: Unknown error occurred[/bold red]")

    except Exception as e:
        console.print("[bold red]Unexpected error during model listing:[/bold red]")
        console.print(f"[red]{e}[/red]")


async def _delete_model(model_id: str) -> None:
    """Delete a model from the local cache.

    Args:
        model_id: Model ID to delete
    """
    console.print(f"[bold yellow]Deleting model: {model_id}[/bold yellow]")

    # Create the delete request
    request = DeleteModelRequest(model_id=model_id)

    try:
        # Use the ModelManager to handle the deletion
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, DeleteModelResultSuccess):
            # Success case
            console.print("[bold green]Model deleted successfully![/bold green]")
            console.print(f"[green]Deleted: {result.deleted_path}[/green]")
        # Failure case

        elif isinstance(result, DeleteModelResultFailure):
            console.print("[bold red]Model deletion failed:[/bold red]")
            if result.result_details:
                console.print(f"[red]{result.result_details}[/red]")
            if result.exception:
                console.print(f"[dim]Error: {result.exception}[/dim]")
        else:
            console.print("[bold red]Model deletion failed: Unknown error occurred[/bold red]")

    except Exception as e:
        console.print("[bold red]Unexpected error during model deletion:[/bold red]")
        console.print(f"[red]{e}[/red]")


def _format_download_row(download: "ModelDownloadStatus") -> tuple[str, str, str, str, str, str]:
    """Format a download status object into table row data.

    Args:
        download: ModelDownloadStatus object

    Returns:
        tuple: (model_id, status_colored, progress_str, size_str, eta_str, started_str)
    """
    progress_str = _format_progress(download)
    size_str = _format_size(download)
    eta_str = _format_eta(download)
    started_str = _format_timestamp(download)
    status_colored = _format_status(download)

    return (
        download.model_id,
        status_colored,
        progress_str,
        size_str,
        eta_str,
        started_str,
    )


def _format_progress(download: "ModelDownloadStatus") -> str:
    """Format download progress information."""
    if download.total_bytes is not None and download.completed_bytes is not None and download.total_bytes > 0:
        progress_percent = (download.completed_bytes / download.total_bytes) * 100
        return f"{progress_percent:.1f}%"
    return "Unknown"


def _format_size(download: "ModelDownloadStatus") -> str:
    """Format download size information."""
    if download.total_bytes is not None and download.completed_bytes is not None:
        return f"{_format_bytes(download.completed_bytes)}/{_format_bytes(download.total_bytes)}"
    return "Unknown"


def _format_bytes(num_bytes: int) -> str:
    """Format bytes to human-readable string."""
    if num_bytes == 0:
        return "0 B"

    bytes_per_unit = 1024.0
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    size = float(num_bytes)

    while size >= bytes_per_unit and unit_index < len(units) - 1:
        size /= bytes_per_unit
        unit_index += 1

    return f"{size:.2f} {units[unit_index]}"


def _format_eta(download: "ModelDownloadStatus") -> str:
    """Format estimated time of arrival."""
    # ETA is not available in the current ModelDownloadStatus structure
    # For active downloads, we could potentially calculate based on progress
    # but without timing data, we return a status-appropriate message
    if download.status == "downloading":
        return "In progress"
    if download.status == "completed":
        return "Completed"
    if download.status == "failed":
        return "Failed"
    return "Unknown"


def _format_timestamp(download: "ModelDownloadStatus") -> str:
    """Format started timestamp."""
    started_at = download.started_at
    if not started_at:
        return "Unknown"

    try:
        from datetime import datetime

        dt = datetime.fromisoformat(started_at)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return started_at[:10]  # Fallback


def _format_status(download: "ModelDownloadStatus") -> str:
    """Format status with color coding."""
    status = download.status
    status_colors = {
        "completed": "green",
        "failed": "red",
        "downloading": "yellow",
    }

    if status in status_colors:
        return f"[{status_colors[status]}]{status}[/{status_colors[status]}]"
    return status


def _display_downloads_table(downloads: list["ModelDownloadStatus"]) -> None:
    """Display downloads in a formatted table.

    Args:
        downloads: List of ModelDownloadStatus objects
    """
    console.print(f"[bold green]Found {len(downloads)} download(s):[/bold green]")

    table = Table()
    table.add_column("Model ID", style="green")
    table.add_column("Status", style="cyan")
    table.add_column("Progress", style="yellow", justify="right")
    table.add_column("Size", style="blue", justify="right")
    table.add_column("ETA", style="magenta", justify="right")
    table.add_column("Started", style="dim")

    for download in downloads:
        row_data = _format_download_row(download)
        table.add_row(*row_data)

    console.print(table)


async def _get_model_status(model_id: str | None) -> None:
    """Get download status for models.

    Args:
        model_id: Optional model ID to get status for
    """
    if model_id:
        console.print(f"[bold green]Getting download status for: {model_id}[/bold green]")
    else:
        console.print("[bold green]Getting download status for all models...[/bold green]")

    # Create the status request
    request = ListModelDownloadsRequest(model_id=model_id)

    try:
        # Use the ModelManager to handle the status query
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, ListModelDownloadsResultSuccess):
            # Success case
            downloads = result.downloads
            if downloads:
                _display_downloads_table(downloads)
            elif model_id:
                console.print(f"[yellow]No download found for model: {model_id}[/yellow]")
            else:
                console.print("[yellow]No downloads found[/yellow]")

        elif isinstance(result, ListModelDownloadsResultFailure):
            console.print("[bold red]Failed to get download status:[/bold red]")
            if result.result_details:
                console.print(f"[red]{result.result_details}[/red]")
            if result.exception:
                console.print(f"[dim]Error: {result.exception}[/dim]")
        else:
            console.print("[bold red]Failed to get download status: Unknown error occurred[/bold red]")

    except Exception as e:
        console.print("[bold red]Unexpected error getting download status:[/bold red]")
        console.print(f"[red]{e}[/red]")


async def _search_models(
    query: str | None,
    task: str | None,
    limit: int,
    sort: str,
    direction: str,
) -> None:
    """Search for models on Hugging Face Hub.

    Args:
        query: Search query to match against model names
        task: Filter by task type
        limit: Maximum number of results
        sort: Sort results by
        direction: Sort direction
    """
    if query:
        console.print(f"[bold green]Searching for models: {query}[/bold green]")
    else:
        console.print("[bold green]Searching for models...[/bold green]")

    # Create the search request
    request = SearchModelsRequest(
        query=query,
        task=task,
        library=None,
        author=None,
        tags=None,
        limit=limit,
        sort=sort,
        direction=direction,
    )

    try:
        # Use the ModelManager to handle the search
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, SearchModelsResultSuccess):
            # Success case
            models = result.models
            if models:
                _display_search_results(models, result.query_info)
            else:
                console.print("[yellow]No models found matching the search criteria[/yellow]")

        elif isinstance(result, SearchModelsResultFailure):
            console.print("[bold red]Model search failed:[/bold red]")
            if result.result_details:
                console.print(f"[red]{result.result_details}[/red]")
            if result.exception:
                console.print(f"[dim]Error: {result.exception}[/dim]")
        else:
            console.print("[bold red]Model search failed: Unknown error occurred[/bold red]")

    except Exception as e:
        console.print("[bold red]Unexpected error during model search:[/bold red]")
        console.print(f"[red]{e}[/red]")


def _display_search_results(models: list[ModelInfo], query_info: QueryInfo) -> None:
    """Display model search results in a formatted table.

    Args:
        models: List of model information
        query_info: Information about the search query
    """
    console.print(f"[bold green]Found {len(models)} models[/bold green]")

    # Show search parameters if any were used
    params = []
    if query_info.query:
        params.append(f"query: {query_info.query}")
    if query_info.task:
        params.append(f"task: {query_info.task}")
    if query_info.library:
        params.append(f"library: {query_info.library}")
    if query_info.author:
        params.append(f"author: {query_info.author}")
    if query_info.tags:
        params.append(f"tags: {', '.join(query_info.tags)}")

    if params:
        console.print(f"[dim]Search parameters: {', '.join(params)}[/dim]")

    table = Table()
    table.add_column("Model ID", style="green")
    table.add_column("Author", style="blue")
    table.add_column("Downloads", style="cyan", justify="right")
    table.add_column("Likes", style="yellow", justify="right")
    table.add_column("Task", style="magenta")
    table.add_column("Library", style="dim")

    for model in models:
        downloads_str = f"{model.downloads:,}" if model.downloads else "0"
        likes_str = str(model.likes or 0)
        task_str = model.task or ""
        library_str = model.library or ""
        author_str = model.author or ""

        table.add_row(
            model.model_id,
            author_str,
            downloads_str,
            likes_str,
            task_str,
            library_str,
        )

    console.print(table)


async def _delete_model_status(model_id: str) -> None:
    """Delete download status records for a model.

    Args:
        model_id: Model ID to delete download status for
    """
    console.print(f"[bold yellow]Deleting download status for: {model_id}[/bold yellow]")

    # Create the delete request
    request = DeleteModelDownloadRequest(model_id=model_id)

    try:
        # Use the ModelManager to handle the deletion
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, DeleteModelDownloadResultSuccess):
            # Success case
            console.print("[bold green]Download status deleted successfully![/bold green]")
            console.print(f"[green]Deleted status file: {result.deleted_path}[/green]")
        # Failure case

        elif isinstance(result, DeleteModelDownloadResultFailure):
            console.print("[bold red]Download status deletion failed:[/bold red]")
            if result.result_details:
                console.print(f"[red]{result.result_details}[/red]")
            if result.exception:
                console.print(f"[dim]Error: {result.exception}[/dim]")
        else:
            console.print("[bold red]Download status deletion failed: Unknown error occurred[/bold red]")

    except Exception as e:
        console.print("[bold red]Unexpected error during download status deletion:[/bold red]")
        console.print(f"[red]{e}[/red]")


if __name__ == "__main__":
    app()
