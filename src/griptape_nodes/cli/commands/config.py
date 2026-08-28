"""Config command for Griptape Nodes CLI."""

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from griptape_nodes.cli.shared import console
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# Imported as a module, not `from ... import USER_CONFIG_PATH`, so the path is read at call
# time. The engine references it the same way, which is what lets the test suite redirect it
# to a temp file instead of touching a real user's config.
from griptape_nodes.retained_mode.managers import config_manager as config_manager_module
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY
from griptape_nodes.utils.library_utils import extract_library_path

config_manager = GriptapeNodes.ConfigManager()

app = typer.Typer(help="Manage configuration.")


@app.command()
def show(
    config_path: str = typer.Argument(
        None,
        help="Optional config path to show specific value (e.g., 'workspace_directory').",
    ),
) -> None:
    """Show configuration values."""
    _print_user_config(config_path)


@app.command("list")
def list_configs() -> None:
    """List configuration values."""
    _list_user_configs()


@app.command()
def reset() -> None:
    """Reset configuration to defaults."""
    _reset_user_config()


@app.command("prune-libraries")
def prune_libraries(
    *,
    remove_outside_root: Annotated[
        bool,
        typer.Option(
            "--remove-outside-root",
            help="Remove entries whose path is not under the resolved libraries directory.",
        ),
    ] = False,
    remove_path: Annotated[
        list[str] | None,
        typer.Option(
            "--remove-path",
            help="Remove one specific entry by its path, exactly as listed. Repeatable.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Inspect (and optionally clean) libraries_to_register in your USER config.

    Editing library settings used to persist the whole merged list, so a user config can
    hold library paths that belong to a project opened long ago. Those entries stay inert
    while a project supplies its own list, then become the engine's library set the moment
    no project layer is loaded to shadow them.

    Reports by default and changes nothing. Only the user config file is ever touched;
    project and workspace configs are read-only here.
    """
    _prune_user_config_libraries(
        remove_outside_root=remove_outside_root,
        remove_paths=remove_path or [],
        assume_yes=yes,
    )


def _print_user_config(config_path: str | None = None) -> None:
    """Prints the user configuration from the config file.

    Args:
        config_path: Optional path to specific config value. If None, prints entire config.
    """
    if config_path is None:
        config = config_manager.merged_config
        sys.stdout.write(json.dumps(config, indent=2))
    else:
        try:
            value = config_manager.get_config_value(config_path)
            if isinstance(value, (dict, list)):
                sys.stdout.write(json.dumps(value, indent=2))
            else:
                sys.stdout.write(str(value))
        except (KeyError, AttributeError, ValueError):
            console.print(f"[bold red]Config path '{config_path}' not found[/bold red]")
            sys.exit(1)


def _list_user_configs() -> None:
    """Lists user configuration files in ascending precedence."""
    num_config_files = len(config_manager.config_files)
    console.print(
        f"[bold]User Configuration Files (lowest precedence (1.) ⟶ highest precedence ({num_config_files}.)):[/bold]"
    )
    for idx, config in enumerate(config_manager.config_files):
        console.print(f"[green]{idx + 1}. {config}[/green]")


def _reset_user_config() -> None:
    """Resets the user configuration to the default values."""
    console.print("[bold]Resetting user configuration to default values...[/bold]")
    config_manager.reset_user_config()
    console.print("[bold green]User configuration reset complete![/bold green]")


def _classify_user_library_entry(path: str, libraries_root: Path) -> str:
    """Describe why a user-config library entry might not belong there.

    Returns a short reason, or an empty string when nothing looks off. These are signals
    rather than proof: registering a library by absolute path from anywhere is legitimate,
    which is why nothing is removed without being asked for explicitly.
    """
    if not path:
        return "no path"
    resolved = Path(path).expanduser()
    if not resolved.exists():
        # The engine already drops these on every load, so seeing one means it has not
        # loaded since the path disappeared.
        return "missing on disk"
    if not resolved.is_relative_to(libraries_root):
        return "outside the libraries directory"
    return ""


def _prune_user_config_libraries(*, remove_outside_root: bool, remove_paths: list[str], assume_yes: bool) -> None:
    """Report, and optionally clean, `libraries_to_register` in the user config."""
    user_entries: list[Any] = (
        config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, config_source="user_config", default=[]) or []
    )
    if not user_entries:
        console.print(
            "[bold green]No libraries_to_register entries in your user config. Nothing to prune.[/bold green]"
        )
        return

    libraries_root = config_manager.resolved_libraries_root()

    # Whatever the other layers supply for this key. Because merge_dicts replaces lists
    # rather than merging them, a project or workspace list does not combine with the
    # user's: it replaces it wholesale, so every entry below is inert while that layer is
    # loaded. Asked through the same helper load_configs uses, so this cannot disagree with
    # the precedence the engine actually resolves.
    shadowing_list = config_manager.get_config_value(
        LIBRARIES_TO_REGISTER_KEY,
        config_source="merged_without_user_config",
        default=[],
    )
    is_shadowed = isinstance(shadowing_list, list) and len(shadowing_list) > 0

    user_config_path = config_manager_module.USER_CONFIG_PATH
    console.print(f"[bold]User config:[/bold] {user_config_path}")
    console.print(f"[bold]Libraries directory:[/bold] {libraries_root}\n")

    if is_shadowed:
        console.print(
            "[yellow]The active project (or workspace) declares its own libraries_to_register, "
            "so none of the entries below are currently in effect. They take over as soon as no "
            "project layer is loaded.[/yellow]\n"
        )

    paths = [extract_library_path(entry) for entry in user_entries]
    reasons = [_classify_user_library_entry(path, libraries_root) for path in paths]

    console.print("[bold]Entries in your user config:[/bold]")
    for path, reason in zip(paths, reasons, strict=True):
        annotation = f"  [yellow]<- {reason}[/yellow]" if reason else ""
        console.print(f"  {path or '(no path)'}{annotation}")

    doomed = {
        path
        for path, reason in zip(paths, reasons, strict=True)
        if (remove_outside_root and reason == "outside the libraries directory") or path in set(remove_paths)
    }

    unmatched = sorted(set(remove_paths) - set(paths))
    for missing_request in unmatched:
        console.print(f"\n[bold red]--remove-path '{missing_request}' matches no entry.[/bold red]")

    if not doomed:
        if not remove_outside_root and not remove_paths:
            console.print(
                "\nNothing changed. Re-run with [bold]--remove-outside-root[/bold] to drop the entries "
                "flagged above, or [bold]--remove-path <path>[/bold] to drop one exactly."
            )
        else:
            console.print("\nNothing matched, so nothing was removed.")
        return

    console.print("\n[bold]Would remove:[/bold]")
    for path in sorted(doomed):
        console.print(f"  [red]{path}[/red]")

    if not assume_yes and not typer.confirm(f"\nRemove {len(doomed)} entry/entries from {user_config_path}?"):
        console.print("[bold]Left unchanged.[/bold]")
        return

    backup_path = user_config_path.with_suffix(".prune.bak")
    shutil.copyfile(user_config_path, backup_path)

    kept = [entry for entry, path in zip(user_entries, paths, strict=True) if path not in doomed]
    if not config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, kept):
        console.print(f"[bold red]Failed to write the user config. It is unchanged; backup at {backup_path}[/bold red]")
        sys.exit(1)

    console.print(f"[bold green]Removed {len(doomed)} entry/entries.[/bold green]")
    console.print(f"Backup of the previous user config: {backup_path}")
