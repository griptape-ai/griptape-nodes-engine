"""Rez commands for Griptape Nodes CLI.

Wire into the app CLI with:
    from griptape_nodes.cli.commands import rez
    app.add_typer(rez.app, name="rez")
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

import griptape_nodes
from griptape_nodes.cli.shared import console
from griptape_nodes.files.path_utils import canonicalize_for_io, strip_windows_long_path_prefix
from griptape_nodes.utils.git_utils import clone_repository
from griptape_nodes.utils.rez_utils import (
    ENV_BIN_PATH,
    ENV_CONFIG_FILE,
    ENV_LOCAL_PACKAGES_PATH,
    ENV_PATH_MAP,
    REZ_PACKAGE_COPY_EXCLUDE_PATTERNS,
    RezSetup,
    _find_package_version_dir,
    _rez_executable,
    build_direct_requires,
    current_platform_key,
    derive_library_version,
    find_library_manifest,
    get_library_rez_package_version,
    install_library_as_rez_package,
    library_rez_family,
    read_library_dependencies,
    read_library_manifest,
    rez_config_dropped_paths,
    rez_package_stores,
    rez_setup,
    rez_subprocess_env,
    rez_unsearched_stores,
)
from griptape_nodes.utils.rez_uv import TORCH_BACKENDS, InstallReport, RezInstallError
from griptape_nodes.utils.rez_uv import current_platform_key as rez_platform_key
from griptape_nodes.utils.rez_uv import install as rez_uv_install

app = typer.Typer(help="Rez package management.")

ENGINE_FAMILY = "griptape_nodes_engine"
LAUNCH_FAMILY = "griptape_launch"
REZ_PLATFORMS = ("linux", "osx", "windows")
RELEASING_GUIDE = "docs/releasing-packages.md in the griptape-rez-demo repository"


@dataclass(frozen=True)
class BuildTarget:
    """The package store a command writes to, and what chose it."""

    store: Path
    source: str


# ---------------------------------------------------------------------------
# build-engine-package command
# ---------------------------------------------------------------------------


@app.command("build-engine-package")
def build_engine_package(
    engine_repo: str = typer.Option(
        None,
        "--engine-repo",
        help="Path to griptape-nodes-engine checkout. Auto-detected if omitted.",
    ),
    local_packages_path: str = typer.Option(
        None,
        "--local-packages-path",
        help=f"Package store to build into. Defaults to {ENV_LOCAL_PACKAGES_PATH}.",
    ),
    rebuild: bool = typer.Option(  # noqa: FBT001
        False,
        "--rebuild",
        help="Reinstall this machine's dependency packages even when they already exist.",
    ),
    yes: bool = typer.Option(  # noqa: FBT001
        False,
        "--yes",
        "-y",
        help="Never prompt (for scripted/CI use). Stops with the reason when something is missing.",
    ),
) -> None:
    """Build the griptape-nodes-engine and every pip dependency as rez packages.

    Needs only rez's tools (GTN_REZ_BIN_PATH) and a package store to build into.
    Works without the engine running.
    """
    interactive = _can_prompt(yes=yes)
    console.print(Panel("[bold cyan]Rez Engine Package Builder[/bold cyan]", expand=False))
    target = _prepare_build(local_packages_path, interactive=interactive)

    repo_path, pyproject_path = _resolve_and_validate_repo(engine_repo, interactive=interactive)
    version, dependencies = _read_project_metadata(pyproject_path)

    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Engine:       [cyan]{repo_path}[/cyan]")
    console.print(f"  Version:      [green]{version}[/green]")
    console.print(f"  Dependencies: [green]{len(dependencies)}[/green] direct")
    console.print(f"  Build into:   [cyan]{target.store}[/cyan]")
    if (target.store / ENGINE_FAMILY / version).exists():
        console.print(f"  [yellow]Existing package {ENGINE_FAMILY}-{version} will be replaced.[/yellow]")
    console.print()

    if interactive and not typer.confirm("Proceed with build?", default=True):
        raise typer.Abort

    target.store.mkdir(parents=True, exist_ok=True)
    _install_deps(dependencies, target.store, skip_installed=not rebuild)
    _write_engine_package(repo_path, target.store, version, dependencies)
    _validate_rez_package(ENGINE_FAMILY, version)
    _print_engine_next_steps(version)


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


def _can_prompt(*, yes: bool) -> bool:
    """Prompts appear only in an interactive terminal, and never with --yes.

    Both ends must be a terminal: on Windows a null stdin (NUL) reports itself as one.
    """
    if yes:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prepare_build(local_packages_path: str | None, *, interactive: bool) -> BuildTarget:
    """Check rez is usable, pick the package store, and report both. Stops with the reason on failure."""
    setup = rez_setup()
    if not setup.enabled:
        _print_setup_panel(setup, target=None)
        reason = setup.disabled_reason or (
            f"Rez is not active: {ENV_BIN_PATH} is not set. Set it to the folder that holds rez's tools (rez-env)."
        )
        console.print(f"[red]{reason}[/red]")
        raise typer.Exit(1)

    target = _resolve_build_target(local_packages_path, setup, interactive=interactive)
    _print_setup_panel(setup, target=target)
    _warn_if_rez_does_not_search(target.store)
    _warn_if_config_replaces_search_path()
    _check_rez_bindings(interactive=interactive)
    return target


def _resolve_build_target(local_packages_path: str | None, setup: RezSetup, *, interactive: bool) -> BuildTarget:
    """The store to build into: the flag, else GTN_REZ_LOCAL_PACKAGES_PATH, else ask (interactive only)."""
    if local_packages_path:
        return BuildTarget(store=_admin_path(local_packages_path), source="--local-packages-path")
    if setup.local_packages_path is not None:
        return BuildTarget(store=setup.local_packages_path, source=ENV_LOCAL_PACKAGES_PATH)
    if not interactive:
        console.print(
            f"[red]Attempted to choose a package store to build into. Failed because neither "
            f"{ENV_LOCAL_PACKAGES_PATH} nor --local-packages-path is set. Griptape Nodes never builds into "
            "your studio's release packages.[/red]"
        )
        raise typer.Exit(1)

    value = typer.prompt(f"Folder to build packages into ({ENV_LOCAL_PACKAGES_PATH})").strip()
    store = _admin_path(value)
    console.print(f"  To skip this question next time: set {ENV_LOCAL_PACKAGES_PATH}={store}")
    return BuildTarget(store=store, source="entered")


def _print_setup_panel(setup: RezSetup, *, target: BuildTarget | None) -> None:
    """Show the rez configuration exactly as the engine reads it."""
    table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    table.add_column(style="bold")
    table.add_column()

    if setup.bin_path is None:
        tools = f"[red]not set[/red] ({ENV_BIN_PATH})"
    elif setup.enabled:
        tools = f"[cyan]{setup.bin_path}[/cyan] ({ENV_BIN_PATH}) [green]rez-env found[/green]"
    else:
        tools = f"[cyan]{setup.bin_path}[/cyan] ({ENV_BIN_PATH}) [red]no rez-env[/red]"
    table.add_row("Rez tools", tools)
    if setup.base is not None:
        table.add_row("Relative to", f"[cyan]{setup.base.path}[/cyan] ({setup.base.source})")
    if target is not None:
        table.add_row("Build into", f"[cyan]{target.store}[/cyan] ({target.source})")
    if setup.config_file is not None:
        table.add_row("Our rezconfig", f"[cyan]{setup.config_file}[/cyan] ({ENV_CONFIG_FILE}), added after yours")
    for warning in setup.warnings:
        table.add_row("[yellow]Ignored[/yellow]", f"[yellow]{warning}[/yellow]")

    console.print()
    console.print(Panel(table, title="Rez setup", expand=False))


def _admin_path(value: str) -> Path:
    r"""Canonicalize a path the administrator typed, in the form they will see and configure.

    These paths are echoed back in the summary and the GTN_REZ_* guidance, so they must
    not carry the Windows ``\\?\`` prefix that ``canonicalize_for_io`` always adds there.
    ``canonicalize_for_identity`` does not fit either: it follows symlinks, which can
    rewrite a mapped studio drive such as ``P:`` to its UNC share.
    """
    return Path(strip_windows_long_path_prefix(canonicalize_for_io(value)))


def _resolve_and_validate_repo(engine_repo_path: str | None, *, interactive: bool) -> tuple[Path, Path]:
    """Locate and validate the engine repo. Asks for it only in an interactive terminal."""
    repo_path = _resolve_engine_repo(engine_repo_path)

    if repo_path is None:
        if not interactive:
            console.print("[red]Pass --engine-repo with the path to your griptape-nodes-engine checkout.[/red]")
            raise typer.Exit(1)
        console.print("The engine repo contains pyproject.toml and src/griptape_nodes/.")
        user_path = typer.prompt("Enter the path to your griptape-nodes-engine checkout")
        repo_path = _admin_path(user_path)
        if not repo_path.is_dir():
            console.print(f"[red]Directory not found: {repo_path}[/red]")
            raise typer.Exit(1)

    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.exists():
        console.print(f"[red]No pyproject.toml found at {pyproject_path}[/red]")
        raise typer.Exit(1)

    return repo_path, pyproject_path


def _warn_if_rez_does_not_search(store: Path) -> None:
    """Warn when the store being built into is not on rez's package search path.

    Read-only: Griptape Nodes never changes rez configuration. Packages in a store rez
    does not search are written correctly but will not resolve with ``rez env``.
    """
    if not rez_unsearched_stores([store]):
        return

    console.print()
    console.print(
        Panel(
            f"[bold yellow]Rez does not search {store}[/bold yellow]\n\n"
            "Packages built there will not resolve with rez-env until the store is added\n"
            "to packages_path in your rez configuration. For example, in a file layered\n"
            f"on your studio configuration with {ENV_CONFIG_FILE} or REZ_CONFIG_FILE:\n\n"
            f'  [cyan]packages_path = ModifyList(append=["{store.as_posix()}"])[/cyan]\n\n'
            "Griptape Nodes never changes your rez configuration.",
            title="Package Store Not On Rez Search Path",
            expand=False,
        )
    )


def _warn_if_config_replaces_search_path() -> None:
    """Warn when GTN_REZ_CONFIG_FILE replaces the studio's package search path instead of extending it."""
    dropped = rez_config_dropped_paths()
    if not dropped:
        return

    console.print()
    console.print(
        Panel(
            f"[bold yellow]Your Griptape Nodes rezconfig ({ENV_CONFIG_FILE}) replaces your studio's "
            "package search path[/bold yellow]\n\n"
            "These studio package folders are no longer searched:\n"
            + "".join(f"\n  [red]x[/red] {path}" for path in dropped)
            + "\n\nUse [cyan]packages_path = ModifyList(append=[...])[/cyan] in that file so it adds to\n"
            "your studio configuration instead.",
            title="Rezconfig Replaces Search Path",
            expand=False,
        )
    )


def _check_rez_bindings(*, interactive: bool = True) -> None:
    """Verify that rez system bindings (platform, os, python) exist.

    These packages are created by ``rez bind`` and are required for rez
    to resolve environments on this machine. Without them, built packages
    will fail to resolve when someone tries to use them.

    When *interactive* is True, asks whether to continue. Otherwise prints a
    warning and proceeds.
    """
    rez_search = _rez_executable("rez-search")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    required = {
        "platform": "rez bind platform",
        "os": "rez bind os",
        f"python-{python_version}": f"rez bind python (version {python_version})",
    }

    missing: list[tuple[str, str]] = []
    for family, bind_hint in required.items():
        try:
            result = subprocess.run(  # noqa: S603
                [rez_search, family], capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=10
            )
            if result.returncode != 0:
                missing.append((family, bind_hint))
        except (OSError, subprocess.SubprocessError):
            missing.append((family, bind_hint))

    if not missing:
        return

    console.print()
    console.print(
        Panel(
            "[bold yellow]Rez system bindings not found[/bold yellow]\n\n"
            "The following rez system packages were not found on rez's package search path:\n"
            + "".join(f"\n  [red]x[/red] [bold]{family}[/bold]" for family, _ in missing)
            + "\n\n"
            "These are created by [cyan]rez bind[/cyan] and tell rez what platform,\n"
            "operating system, and Python version are available on this machine.\n"
            "Without them, built packages cannot be resolved into environments.\n\n"
            "To fix this, run:\n" + "".join(f"\n  [cyan]{hint}[/cyan]" for _, hint in missing) + "\n\n"
            "If you are building packages to copy to another machine (e.g. a shared\n"
            "network store), you can skip this check and bind on the target instead.",
            title="Missing Rez Bindings",
            expand=False,
        )
    )

    if not interactive:
        console.print("[yellow]Proceeding without bindings.[/yellow]")
        console.print()
        return

    if not typer.confirm("Continue building without rez bindings?", default=False):
        raise typer.Abort

    console.print()


# ---------------------------------------------------------------------------
# Engine build steps
# ---------------------------------------------------------------------------


def _read_project_metadata(pyproject_path: Path) -> tuple[str, list[str]]:
    """Read version and dependencies from pyproject.toml."""
    version = _read_version(pyproject_path)
    if version is None:
        console.print("[red]Could not read version from pyproject.toml[/red]")
        raise typer.Exit(1)

    dependencies = _read_dependencies(pyproject_path)
    return version, dependencies


def _install_deps(dependencies: list[str], store: Path, *, skip_installed: bool) -> None:
    """Install pip dependencies as individual rez packages."""
    if not dependencies:
        console.print("[yellow]No pip dependencies found — skipping dependency install.[/yellow]")
        return

    console.print("[bold]Installing dependencies as rez packages...[/bold]")
    try:
        report = rez_uv_install(
            dependencies,
            packages_dir=store,
            skip_installed=skip_installed,
        )
    except subprocess.CalledProcessError as exc:
        console.print("[red]Attempted to install the engine's dependencies as rez packages. Failed due to:[/red]")
        _print_uv_error(exc)
        raise typer.Exit(1) from exc
    except RezInstallError as exc:
        console.print("[red]Attempted to install the engine's dependencies as rez packages. Failed due to:[/red]")
        _print_install_failures(exc)
        raise typer.Exit(1) from exc
    except OSError as exc:
        console.print(
            f"[red]Attempted to install the engine's dependencies as rez packages. Failed due to: {exc}[/red]"
        )
        raise typer.Exit(1) from exc

    console.print("[green]Dependencies installed.[/green]")
    _print_install_report(report)
    console.print()


def _print_install_report(report: InstallReport) -> None:
    """Say what the build added to the store, and which other platforms it already served."""
    this_machine = rez_platform_key().replace("-", "/", 1)
    if report.other_platforms:
        others = ", ".join(sorted(report.other_platforms))
        console.print(f"  This store already has packages built for {others}.")
    parts = [f"{len(report.new)} new"]
    if report.added_variant:
        parts.append(f"{len(report.added_variant)} gained a {this_machine} variant")
    if report.rebuilt:
        parts.append(f"{len(report.rebuilt)} rebuilt")
    parts.append(f"{len(report.already_installed)} already installed for {this_machine}")
    console.print(f"  Packages: {'; '.join(parts)}.")


def _print_install_failures(exc: RezInstallError) -> None:
    """List the packages that could not be installed, and why."""
    console.print(f"  {len(exc.failures)} package(s) could not be installed:")
    for failure in exc.failures:
        console.print(f"  [dim]- {failure}[/dim]")
    console.print("  The package was not written, so nothing references the missing packages.")


def _print_uv_error(exc: subprocess.CalledProcessError) -> None:
    """Print the tail of uv's error output, which names the package it could not resolve."""
    stderr = (exc.stderr or "").strip()
    if not stderr:
        console.print(f"  [dim]uv exited with status {exc.returncode}[/dim]")
        return
    for line in stderr.splitlines()[-10:]:
        console.print(f"  [dim]{line}[/dim]")


def _write_engine_package(
    repo_path: Path,
    store: Path,
    version: str,
    dependencies: list[str],
) -> None:
    """Write the engine meta-package with source, .dist-info, and requires."""
    console.print("[bold]Building engine meta-package...[/bold]")
    version_dir = store / ENGINE_FAMILY / version

    if version_dir.exists():
        console.print(f"  [yellow]Package already exists at {version_dir} — replacing.[/yellow]")
        shutil.rmtree(version_dir, ignore_errors=True)

    version_dir.mkdir(parents=True, exist_ok=True)

    python_dest = version_dir / "python"
    src_dir = repo_path / "src"
    if not src_dir.is_dir():
        console.print(f"[red]Engine source directory not found: {src_dir}[/red]")
        raise typer.Exit(1)

    console.print(f"  Copying engine source: [cyan]{src_dir}[/cyan]")
    _copy_engine_source(src_dir, python_dest)
    _copy_dist_info(python_dest, version)

    requires = build_direct_requires(dependencies)

    timestamp = datetime.now(UTC).isoformat()
    req_entries = "".join(f"    '{r}',\n" for r in sorted(requires))
    content = f"""\
# -*- coding: utf-8 -*-
# Generated by Griptape Nodes (rez build-engine-package)
# Source: {repo_path}
# Built: {timestamp}

name = '{ENGINE_FAMILY}'

version = '{version}'

description = 'Griptape Nodes Engine'

requires = [
{req_entries}]

format_version = 2


def commands():
    env.PYTHONPATH.append('{{root}}/python')
"""
    pkg_file = version_dir / "package.py"
    pkg_file.write_text(content, encoding="utf-8")

    console.print()
    console.print(
        Panel(
            f"[bold green]Engine package built successfully![/bold green]\n\n"
            f"  Package: [cyan]{ENGINE_FAMILY}-{version}[/cyan]\n"
            f"  Location: [cyan]{version_dir}[/cyan]\n"
            f"  Dependencies: [cyan]{len(requires)}[/cyan] direct requires",
            title="Success",
            expand=False,
        )
    )


def _print_engine_next_steps(version: str) -> None:
    """Print verification and next-step commands."""
    console.print()
    table = Table(title="Next Steps", show_header=False, show_edge=False, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Verify:", f"rez-search {ENGINE_FAMILY}")
    table.add_row("Test:", f'rez-env {ENGINE_FAMILY}-{version} -- python -c "import griptape_nodes"')
    table.add_row("Launch:", f"write-launch-package, then rez-env {LAUNCH_FAMILY} -- gtn")
    table.add_row("Release:", f"see {RELEASING_GUIDE}")
    console.print(table)


def _resolve_engine_repo(explicit_path: str | None) -> Path | None:
    """Find the engine repo, checking explicit path, then common locations."""
    if explicit_path:
        p = _admin_path(explicit_path)
        if p.is_dir():
            return p
        console.print(f"[red]Engine repo not found at: {p}[/red]")
        return None

    # When running from a checkout (editable install), the package's own location gives the
    # repo root: <repo>/src/griptape_nodes/__init__.py.
    candidate = Path(griptape_nodes.__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate

    home = Path.home()
    candidates = [
        Path.cwd(),
        home / "work" / "griptape-nodes-engine",
        home / "griptape-nodes-engine",
    ]
    env_repo = os.environ.get("GRIPTAPE_ENGINE_REPO")
    if env_repo:
        candidates.insert(0, Path(env_repo))

    for candidate in candidates:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "griptape_nodes").is_dir():
            return candidate.resolve()

    console.print("[yellow]Could not auto-detect the engine repository.[/yellow]")
    return None


def _read_version(pyproject_path: Path) -> str | None:
    """Read the version from pyproject.toml."""
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    version = data.get("project", {}).get("version")
    if version:
        return str(version)

    version = data.get("tool", {}).get("poetry", {}).get("version")
    if version:
        return str(version)

    return None


def _read_dependencies(pyproject_path: Path) -> list[str]:
    """Read pip dependencies from pyproject.toml."""
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    return list(data.get("project", {}).get("dependencies", []))


_ENGINE_COPY_EXCLUDES = shutil.ignore_patterns(
    *REZ_PACKAGE_COPY_EXCLUDE_PATTERNS,
    ".ruff_cache",
    ".mypy_cache",
)


def _copy_engine_source(src_dir: Path, dest: Path) -> None:
    """Copy engine source tree into the rez package."""
    shutil.copytree(
        src_dir,
        dest,
        ignore=_ENGINE_COPY_EXCLUDES,
        dirs_exist_ok=True,
    )


def _copy_dist_info(python_dest: Path, version: str) -> None:
    """Create .dist-info for importlib.metadata version discovery.

    The engine needs importlib.metadata.version("griptape-nodes-engine") to
    work inside rez environments. We generate a minimal .dist-info directory
    with METADATA containing the version.
    """
    dist_info_name = f"griptape_nodes_engine-{version}.dist-info"
    dist_info_dir = python_dest / dist_info_name
    dist_info_dir.mkdir(parents=True, exist_ok=True)

    metadata_content = f"Metadata-Version: 2.1\nName: griptape-nodes-engine\nVersion: {version}\n"
    (dist_info_dir / "METADATA").write_text(metadata_content, encoding="utf-8")
    (dist_info_dir / "top_level.txt").write_text("griptape_nodes\n", encoding="utf-8")
    (dist_info_dir / "INSTALLER").write_text("gtn-rez\n", encoding="utf-8")

    console.print(f"  Created .dist-info: [cyan]{dist_info_name}[/cyan]")


# ---------------------------------------------------------------------------
# build-library-package command
# ---------------------------------------------------------------------------


@app.command("build-library-package")
def build_library_package(  # noqa: PLR0913, PLR0917
    git_url: str = typer.Option(None, "--git-url", help="Git URL to clone and build."),
    branch: str = typer.Option(None, "--branch", help="Branch, tag, or commit to checkout."),
    local_path: str = typer.Option(None, "--local-path", help="Local path to library directory."),
    local_packages_path: str = typer.Option(
        None,
        "--local-packages-path",
        help=f"Package store to build into. Defaults to {ENV_LOCAL_PACKAGES_PATH}.",
    ),
    rebuild: bool = typer.Option(  # noqa: FBT001
        False,
        "--rebuild",
        help="Reinstall this machine's dependency packages and the library package even when they exist.",
    ),
    torch_backend: Annotated[
        list[str] | None,
        typer.Option(
            "--torch-backend",
            help=(
                "Torch build to install, e.g. cu118 or cu128 (repeatable), or 'all' for every build that "
                "publishes the library's torch version. Each becomes its own variant, and workstations pick "
                "theirs. Replaces the library's own torch source. Windows and Linux only."
            ),
        ),
    ] = None,
    yes: bool = typer.Option(  # noqa: FBT001
        False,
        "--yes",
        "-y",
        help="Never prompt (for scripted/CI use). Stops with the reason when something is missing.",
    ),
) -> None:
    """Build a rez package for a Griptape node library.

    Accepts either a git URL (clones and builds) or a local directory path.
    Reads the library manifest for name and dependencies, installs each
    dependency as a rez package, then writes the library meta-package.

    Works without the engine running.
    """
    if git_url and local_path:
        console.print("[red]Provide either --git-url or --local-path, not both.[/red]")
        raise typer.Exit(1)
    if not git_url and not local_path:
        console.print("[red]Provide --git-url or --local-path.[/red]")
        raise typer.Exit(1)

    backends = _torch_backends(torch_backend)
    interactive = _can_prompt(yes=yes)
    console.print(Panel("[bold cyan]Rez Library Package Builder[/bold cyan]", expand=False))
    target = _prepare_build(local_packages_path, interactive=interactive)
    options = LibraryBuildOptions(
        store=target.store, skip_installed=not rebuild, interactive=interactive, torch_backends=backends
    )

    if git_url:
        _build_library_from_git(git_url, branch, options)
    else:
        _build_library_from_local(local_path, options)  # type: ignore[arg-type]


@dataclass(frozen=True)
class LibraryBuildOptions:
    """Settings shared by a library build and the library dependencies it builds."""

    store: Path
    skip_installed: bool
    interactive: bool
    torch_backends: list[str] | None = None


def _torch_backends(values: list[str] | None) -> list[str] | None:
    """Validate --torch-backend values. None on macOS, which has a single torch build."""
    if not values:
        return None
    if "all" in values:
        backends = ["all"]
    else:
        unknown = [value for value in values if value not in TORCH_BACKENDS]
        if unknown:
            console.print(
                f"[red]Attempted to use --torch-backend {', '.join(unknown)}. Failed because it is not a torch "
                f"build uv knows ({', '.join(TORCH_BACKENDS)}, or all).[/red]"
            )
            raise typer.Exit(1)
        backends = list(dict.fromkeys(values))
    if current_platform_key() == "osx":
        console.print("[yellow]--torch-backend is ignored on macOS, which has a single torch build.[/yellow]")
        return None
    return backends


def _build_library_from_git(git_url: str, branch: str | None, options: LibraryBuildOptions) -> None:
    """Clone a git repo and build a rez package from its library manifest."""
    console.print()
    console.print(f"  Git URL: [cyan]{git_url}[/cyan]")
    if branch:
        console.print(f"  Branch:  [cyan]{branch}[/cyan]")

    temp_dir = Path(tempfile.mkdtemp(prefix="rez_lib_build_"))
    clone_target = temp_dir / "repo"

    try:
        console.print("[bold]Cloning repository...[/bold]")
        clone_repository(git_url, clone_target, branch)
        console.print("[green]Clone complete.[/green]")
        console.print()

        _build_library_from_dir(clone_target, options)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_library_from_local(local_path: str, options: LibraryBuildOptions) -> None:
    """Build a rez package from a local library directory."""
    library_dir = _admin_path(local_path)
    if not library_dir.is_dir():
        console.print(f"[red]Directory not found: {library_dir}[/red]")
        raise typer.Exit(1)

    console.print()
    console.print(f"  Library dir: [cyan]{library_dir}[/cyan]")
    console.print()

    _build_library_from_dir(library_dir, options)


def _build_library_from_dir(library_dir: Path, options: LibraryBuildOptions) -> None:  # noqa: C901, PLR0912, PLR0915
    """Build a rez package from a directory containing a library manifest.

    Also recursively builds rez packages for any declared library dependencies.
    """
    library_json = find_library_manifest(library_dir)
    if library_json is None:
        console.print(f"[red]No library manifest found in {library_dir}[/red]")
        raise typer.Exit(1)

    library_name, pip_dependencies, pip_dependencies_exec, pip_install_flags = read_library_manifest(library_json)
    if not library_name:
        console.print("[red]Could not read library name from manifest.[/red]")
        raise typer.Exit(1)

    library_deps = read_library_dependencies(library_json)
    naming = library_rez_family(library_json)
    library_version = derive_library_version(library_json)

    console.print("[bold]Summary:[/bold]")
    console.print(f"  Library:       [green]{library_name}[/green]")
    console.print(f"  Package:       [cyan]{naming.family}-{library_version}[/cyan] (named after {naming.source})")
    console.print(f"  Manifest:      [cyan]{library_json}[/cyan]")
    console.print(f"  Pip deps:      [green]{len(pip_dependencies)} edit + {len(pip_dependencies_exec)} exec[/green]")
    if pip_install_flags:
        console.print(f"  Install flags: [green]{' '.join(pip_install_flags)}[/green]")
    if options.torch_backends:
        console.print(f"  Torch builds:  [green]{', '.join(options.torch_backends)}[/green]")
    if library_deps:
        console.print(f"  Library deps:  [green]{len(library_deps)}[/green] (built first)")
    console.print(f"  Build into:    [cyan]{options.store}[/cyan]")
    console.print()

    if options.interactive and not typer.confirm("Proceed with build?", default=True):
        raise typer.Abort

    # Recursively build rez packages for library dependencies first. They are built
    # without further questions, and an existing package is kept.
    if library_deps:
        console.print("[bold]Building library dependencies...[/bold]")
        dependency_options = LibraryBuildOptions(
            store=options.store, skip_installed=True, interactive=False, torch_backends=options.torch_backends
        )
        for lib_dep in library_deps:
            dep_url = lib_dep["url"]
            console.print(f"  Dependency: [cyan]{dep_url}[/cyan]")
            try:
                _build_library_from_git(str(dep_url), None, dependency_options)
            except (typer.Exit, typer.Abort):
                if lib_dep.get("required", False):
                    console.print(f"[red]Required library dependency failed: {dep_url}[/red]")
                    raise
                console.print(f"[yellow]Optional library dependency failed: {dep_url} — continuing[/yellow]")
        console.print()

    if not pip_dependencies:
        console.print("[yellow]No pip dependencies found — skipping dependency install.[/yellow]")

    console.print(f"[bold]Building rez package for '{library_name}'...[/bold]")
    try:
        result = install_library_as_rez_package(
            library_name,
            pip_dependencies,
            pip_dependencies_exec=pip_dependencies_exec or None,
            library_file_path=library_json,
            pip_install_flags=pip_install_flags or None,
            skip_installed=options.skip_installed,
            store=options.store,
            torch_backends=options.torch_backends,
        )
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Attempted to build a rez package for '{library_name}'. Failed due to:[/red]")
        _print_uv_error(exc)
        raise typer.Exit(1) from exc
    except RezInstallError as exc:
        console.print(f"[red]Attempted to build a rez package for '{library_name}'. Failed due to:[/red]")
        _print_install_failures(exc)
        raise typer.Exit(1) from exc
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Attempted to build a rez package for '{library_name}'. Failed due to: {exc}[/red]")
        raise typer.Exit(1) from exc

    if result is not None:
        _print_install_report(result.report)
        if result.built_torch_backends:
            console.print(f"  Torch builds installed: {', '.join(result.built_torch_backends)}")
        if result.skipped_torch_backends:
            console.print(
                f"  Torch builds skipped (they do not publish the pinned torch version): "
                f"{', '.join(result.skipped_torch_backends)}"
            )

    family = naming.family
    version = get_library_rez_package_version(library_json, store=options.store)

    console.print()
    console.print(
        Panel(
            f"[bold green]Library package built successfully![/bold green]\n\n"
            f"  Package: [cyan]{family}-{version}[/cyan]\n"
            f"  REZ ref: [cyan]REZ:{family}-{version}[/cyan]",
            title="Success",
            expand=False,
        )
    )
    _validate_rez_package(family, version)
    _print_library_next_steps(family, version)


def _validate_rez_package(family: str, version: str | None) -> None:
    """Validate a generated package by querying the rez binary."""
    rez_search = _rez_executable("rez-search")
    console.print("[bold]Validating rez package...[/bold]")

    search_cmd = [rez_search, family]
    try:
        result = subprocess.run(  # noqa: S603
            search_cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=15
        )
        if result.returncode == 0:
            console.print(f"  [green]rez-search {family}: found[/green]")
        else:
            console.print(f"  [yellow]rez-search {family}: not found (rez may not search this store)[/yellow]")
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"  [yellow]rez-search failed: {exc}[/yellow]")

    if version:
        spec = f"{family}-{version}"
        rez_bin = _rez_executable("rez")
        resolve_cmd = [rez_bin, "env", spec, "--", "echo", "ok"]
        try:
            result = subprocess.run(  # noqa: S603
                resolve_cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=30
            )
            if result.returncode == 0 and "ok" in result.stdout:
                console.print(f"  [green]rez-env {spec}: resolves successfully[/green]")
            else:
                console.print(f"  [yellow]rez-env {spec}: did not resolve cleanly[/yellow]")
                if result.stderr.strip():
                    for line in result.stderr.strip().splitlines()[-3:]:
                        console.print(f"    [dim]{line}[/dim]")
        except (OSError, subprocess.SubprocessError) as exc:
            console.print(f"  [yellow]rez-env resolve failed: {exc}[/yellow]")

    console.print()


def _print_library_next_steps(family: str, version: str | None) -> None:
    """Print verification steps for a built library package."""
    console.print()
    table = Table(title="Next Steps", show_header=False, show_edge=False, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Verify:", f"rez-search {family}")
    ref = f"REZ:{family}-{version}" if version else f"REZ:{family}"
    table.add_row("Config:", f'Add "{ref}" to libraries_to_register')
    table.add_row("Release:", f"see {RELEASING_GUIDE}")
    console.print(table)


# ---------------------------------------------------------------------------
# write-launch-package command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchSettings:
    """What a griptape_launch package sets on each platform."""

    per_platform: dict[str, dict[str, str]]
    shared: dict[str, str]
    engine_version: str


@app.command("write-launch-package")
def write_launch_package(  # noqa: PLR0913, PLR0917
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help=(
                "Where rez's tools are on a platform, as platform=path (repeatable), e.g. "
                "--tools windows=P:/rez/bin --tools osx=/Volumes/pipeline/rez/bin. A bare path means this "
                f"platform. Becomes {ENV_BIN_PATH}. Defaults to this machine's {ENV_BIN_PATH}."
            ),
        ),
    ] = None,
    root: Annotated[
        list[str] | None,
        typer.Option(
            "--root",
            help=f"Optional base for relative paths, as platform=path (repeatable). Becomes {ENV_PATH_MAP}.",
        ),
    ] = None,
    testing_store: Annotated[
        list[str] | None,
        typer.Option(
            "--testing-store",
            help=(
                "Make a testing launch package that also uses a local package store, as platform=path "
                f"(repeatable). Becomes {ENV_LOCAL_PACKAGES_PATH}. Leave out for production."
            ),
        ),
    ] = None,
    config_file: Annotated[
        list[str] | None,
        typer.Option(
            "--config-file",
            help=f"Testing only: Griptape Nodes rezconfig, as platform=path (repeatable). Becomes {ENV_CONFIG_FILE}.",
        ),
    ] = None,
    set_env: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Extra environment variable for every platform, as NAME=VALUE (repeatable).",
        ),
    ] = None,
    engine_version: str = typer.Option(
        None,
        "--engine-version",
        help=f"Engine version to require. Defaults to the newest {ENGINE_FAMILY} rez can find.",
    ),
    version: str = typer.Option(
        None,
        "--version",
        help=f"Version of the {LAUNCH_FAMILY} package. Defaults to one patch above the newest in the store.",
    ),
    local_packages_path: str = typer.Option(
        None,
        "--local-packages-path",
        help=f"Package store to write the launch package into. Defaults to {ENV_LOCAL_PACKAGES_PATH}.",
    ),
    rebuild: bool = typer.Option(  # noqa: FBT001
        False,
        "--rebuild",
        help="Replace an existing launch package of the same version.",
    ),
    print_only: bool = typer.Option(  # noqa: FBT001
        False,
        "--print",
        help="Show the package instead of writing it.",
    ),
    yes: bool = typer.Option(  # noqa: FBT001
        False,
        "--yes",
        "-y",
        help="Never prompt (for scripted/CI use). Stops with the reason when something is missing.",
    ),
) -> None:
    """Write the griptape_launch rez package that configures Griptape Nodes on every workstation.

    Artists start Griptape Nodes with `rez-env griptape_launch -- gtn`. The package sets the
    GTN_REZ_* variables for each platform. It is written to the local store only; release it
    like any other package.
    """
    interactive = _can_prompt(yes=yes)
    console.print(Panel("[bold cyan]Rez Launch Package Writer[/bold cyan]", expand=False))
    setup = rez_setup()

    tool_paths = _platform_values(tools, "--tools")
    if not tool_paths and interactive:
        tool_paths = _prompt_platform_values("Where are rez's tools", setup.bin_path)
    if not tool_paths and setup.bin_path is not None:
        tool_paths = {current_platform_key(): str(setup.bin_path)}
    if not tool_paths:
        console.print(
            f"[red]Attempted to write a launch package. Failed because no rez tools folder was given. "
            f"Pass --tools platform=path or set {ENV_BIN_PATH}.[/red]"
        )
        raise typer.Exit(1)

    settings = LaunchSettings(
        per_platform=_per_platform_settings(
            tool_paths=tool_paths,
            testing_stores=_platform_values(testing_store, "--testing-store"),
            config_files=_platform_values(config_file, "--config-file"),
        ),
        shared=_shared_settings(_platform_values(root, "--root"), set_env),
        engine_version=engine_version or _newest_engine_version(local_packages_path, setup),
    )
    _check_relative_tools(settings)

    target: BuildTarget | None = None
    if not print_only:
        target = _resolve_build_target(local_packages_path, setup, interactive=interactive)
    launch_version = version or _next_launch_version(target)
    content = _launch_package_content(settings, launch_version)

    if print_only:
        # Plain text: the console would wrap long lines and break the generated code.
        sys.stdout.write(content)
        return

    if target is None:
        msg = "A package store is required to write the launch package."
        raise RuntimeError(msg)
    package_file = target.store / LAUNCH_FAMILY / launch_version / "package.py"
    if package_file.exists() and not rebuild:
        console.print(
            f"[red]Attempted to write {LAUNCH_FAMILY}-{launch_version}. Failed because it already exists at "
            f"{package_file}. Pass --version for a new version, or --rebuild to replace it.[/red]"
        )
        raise typer.Exit(1)

    package_file.parent.mkdir(parents=True, exist_ok=True)
    package_file.write_text(content, encoding="utf-8")
    console.print(f"  Wrote [cyan]{package_file}[/cyan]")
    _warn_if_rez_does_not_search(target.store)
    _validate_launch_package(launch_version)


def _platform_values(values: list[str] | None, option: str) -> dict[str, str]:
    """Parse repeatable platform=path options. A bare path means this platform."""
    result: dict[str, str] = {}
    for raw in values or []:
        platform, separator, path = raw.partition("=")
        if separator and platform in REZ_PLATFORMS:
            result[platform] = path.strip()
            continue
        if separator:
            console.print(
                f"[red]Attempted to read {option} {raw}. Failed because '{platform}' is not a rez platform "
                f"({', '.join(REZ_PLATFORMS)}).[/red]"
            )
            raise typer.Exit(1)
        result[current_platform_key()] = raw.strip()
    return result


def _prompt_platform_values(question: str, current: Path | None) -> dict[str, str]:
    """Ask for a path on each platform; blank skips a platform."""
    result: dict[str, str] = {}
    this_platform = current_platform_key()
    for platform in REZ_PLATFORMS:
        default = str(current) if platform == this_platform and current is not None else ""
        value = typer.prompt(f"{question} on {platform}? (blank to skip)", default=default, show_default=bool(default))
        if value.strip():
            result[platform] = value.strip()
    return result


def _per_platform_settings(
    *,
    tool_paths: dict[str, str],
    testing_stores: dict[str, str],
    config_files: dict[str, str],
) -> dict[str, dict[str, str]]:
    per_platform: dict[str, dict[str, str]] = {}
    for platform, path in tool_paths.items():
        values = {ENV_BIN_PATH: path}
        if platform in testing_stores:
            values[ENV_LOCAL_PACKAGES_PATH] = testing_stores[platform]
        if platform in config_files:
            values[ENV_CONFIG_FILE] = config_files[platform]
        per_platform[platform] = values
    return per_platform


def _shared_settings(roots: dict[str, str], extra: list[str] | None) -> dict[str, str]:
    shared: dict[str, str] = {}
    if roots:
        shared[ENV_PATH_MAP] = ";".join(f"{platform}={path}" for platform, path in sorted(roots.items()))
    for raw in extra or []:
        name, separator, value = raw.partition("=")
        if not separator or not name.strip():
            console.print(f"[red]Attempted to read --set {raw}. Failed because it is not NAME=VALUE.[/red]")
            raise typer.Exit(1)
        shared[name.strip()] = value
    return shared


def _check_relative_tools(settings: LaunchSettings) -> None:
    """A relative tools path needs a --root for that platform, or rez stays off there."""
    roots = settings.shared.get(ENV_PATH_MAP, "")
    rooted = {entry.partition("=")[0] for entry in roots.split(";") if entry}
    for platform, values in settings.per_platform.items():
        tools = values[ENV_BIN_PATH]
        if _is_absolute_for(tools, platform) or platform in rooted:
            continue
        console.print(
            f"[red]Attempted to use the relative tools path '{tools}' on {platform}. Failed because no --root "
            f"is given for {platform}, so rez would not be active there. Use an absolute path or add "
            f"--root {platform}=<path>.[/red]"
        )
        raise typer.Exit(1)


def _is_absolute_for(path: str, platform: str) -> bool:
    """Whether *path* is absolute on *platform* (checked by shape, from any machine)."""
    if platform == "windows":
        has_drive = re.match(r"^[A-Za-z]:", path) is not None
        return has_drive or path.startswith(("\\\\", "//"))
    return path.startswith("/")


def _newest_engine_version(local_packages_path: str | None, setup: RezSetup) -> str:
    """The newest engine package in the target store or on rez's search path."""
    stores = list(rez_package_stores())
    if local_packages_path:
        stores.insert(0, _admin_path(local_packages_path))
    elif setup.local_packages_path is not None:
        stores.insert(0, setup.local_packages_path)
    version_dir = _find_package_version_dir(ENGINE_FAMILY, None, stores)
    if version_dir is None:
        console.print(
            f"[red]Attempted to find a {ENGINE_FAMILY} package to require. Failed because none was found. "
            "Build it first with build-engine-package, or pass --engine-version.[/red]"
        )
        raise typer.Exit(1)
    return version_dir.name


def _next_launch_version(target: BuildTarget | None) -> str:
    """One patch above the newest launch package in the store, or 1.0.0."""
    if target is None:
        return "1.0.0"
    newest = _find_package_version_dir(LAUNCH_FAMILY, None, [target.store])
    if newest is None:
        return "1.0.0"
    parts = newest.name.split(".")
    if not parts[-1].isdigit():
        return f"{newest.name}.1"
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _launch_package_content(settings: LaunchSettings, launch_version: str) -> str:
    """The package.py text. Values are chosen per platform when the package resolves."""
    platform_lines = []
    for platform in sorted(settings.per_platform):
        values = settings.per_platform[platform]
        entries = ", ".join(f"{name!r}: {value!r}" for name, value in values.items())
        platform_lines.append(f"        {platform!r}: {{{entries}}},")
    shared_lines = "".join(f"    setenv({name!r}, {value!r})\n" for name, value in sorted(settings.shared.items()))
    platforms = ", ".join(sorted(settings.per_platform))
    timestamp = datetime.now(UTC).isoformat()
    return f"""\
# -*- coding: utf-8 -*-
# Generated by Griptape Nodes (rez write-launch-package) on {timestamp}.
# Start Griptape Nodes with: rez-env {LAUNCH_FAMILY} -- gtn

name = '{LAUNCH_FAMILY}'

version = '{launch_version}'

description = 'Configures and launches Griptape Nodes inside rez.'

requires = ['{ENGINE_FAMILY}-{settings.engine_version}']

format_version = 2


def commands():
    settings = {{
{chr(10).join(platform_lines)}
    }}
    if system.platform not in settings:
        stop('{LAUNCH_FAMILY} has no rez tools folder for platform ' + system.platform + ' (configured: {platforms}).')
    for name, value in settings[system.platform].items():
        setenv(name, value)
{shared_lines}"""


def _validate_launch_package(launch_version: str) -> None:
    """Resolve the launch package on this machine and import the engine inside it."""
    spec = f"{LAUNCH_FAMILY}-{launch_version}"
    cmd = [_rez_executable("rez"), "env", spec, "--", "python", "-c", "import griptape_nodes"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"  [yellow]Could not check {spec}: {exc}[/yellow]")
        return
    if result.returncode == 0:
        console.print(f"  [green]rez-env {spec}: resolves and imports griptape_nodes[/green]")
        console.print(f"  Start Griptape Nodes with: [cyan]rez-env {LAUNCH_FAMILY} -- gtn[/cyan]")
        console.print(f"  Release it like any other package: see {RELEASING_GUIDE}")
        return
    console.print(f"  [yellow]rez-env {spec} did not resolve on this machine:[/yellow]")
    for line in result.stderr.strip().splitlines()[-5:]:
        console.print(f"    [dim]{line}[/dim]")
