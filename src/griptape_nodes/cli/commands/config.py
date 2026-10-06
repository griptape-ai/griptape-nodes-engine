"""Config command for Griptape Nodes CLI."""

import json
import sys

import typer

from griptape_nodes.cli.shared import console
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

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
    # ASCII arrow only: "\u27f6" raises UnicodeEncodeError on consoles whose
    # stdout encoding is cp1252 (see issue #5470).
    console.print(
        f"[bold]User Configuration Files (lowest precedence (1.) -> highest precedence ({num_config_files}.)):[/bold]"
    )
    for idx, config in enumerate(config_manager.config_files):
        console.print(f"[green]{idx + 1}. {config}[/green]")


def _reset_user_config() -> None:
    """Resets the user configuration to the default values."""
    console.print("[bold]Resetting user configuration to default values...[/bold]")
    config_manager.reset_user_config()
    console.print("[bold green]User configuration reset complete![/bold green]")
