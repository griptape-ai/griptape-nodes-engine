"""Rez commands for Griptape Nodes CLI.

Wire into the app CLI with:
    from griptape_nodes.cli.commands import rez
    app.add_typer(rez.app, name="rez")
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

import griptape_nodes
from griptape_nodes.cli.shared import console
from griptape_nodes.files.path_utils import canonicalize_for_io, strip_windows_long_path_prefix
from griptape_nodes.utils.git_utils import clone_repository
from griptape_nodes.utils.rez_utils import (
    REZ_PACKAGE_COPY_EXCLUDE_PATTERNS,
    _rez_executable,
    build_direct_requires,
    current_platform_key,
    find_library_manifest,
    get_library_rez_package_version,
    install_library_as_rez_package,
    library_file_path_to_rez_family,
    read_library_dependencies,
    read_library_manifest,
    rez_local_packages_path,
    rez_path_map,
    rez_root,
    rez_subprocess_env,
    rez_unsearched_stores,
)
from griptape_nodes.utils.rez_uv import install as rez_uv_install

app = typer.Typer(help="Rez package management.")


@app.command("build-engine-package")
def build_engine_package(
    engine_repo: str = typer.Option(
        None,
        "--engine-repo",
        help="Path to griptape-nodes-engine checkout. Auto-detected if omitted.",
    ),
    packages_path: str = typer.Option(
        None,
        "--packages-path",
        help=(
            "Rez package store root; packages are written to <path>/local. "
            "Used with --yes; falls back to GTN_REZ_ROOT. The interactive wizard asks instead."
        ),
    ),
    skip_installed: bool = typer.Option(  # noqa: FBT001
        True,
        "--skip-installed/--reinstall",
        help="Skip dependencies whose rez package already exists.",
    ),
    yes: bool = typer.Option(  # noqa: FBT001
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt (for scripted/CI use).",
    ),
) -> None:
    """Build the griptape-nodes-engine as a rez package.

    Wizard-style command for rez administrators. Walks through studio root
    configuration, then builds the engine and all its pip dependencies as
    individual rez packages.

    Works without the engine running.
    """
    repo_path, pyproject_path = _resolve_and_validate_repo(engine_repo)
    version, dependencies = _read_project_metadata(pyproject_path)

    if yes:
        cross_platform_roots = None
        studio_root, rez_paths = _resolve_studio_root_from_env_or_option(packages_path)
    else:
        cross_platform_roots, studio_root, rez_paths = _prompt_studio_setup()

    packages_root = studio_root / rez_paths["local_packages"]

    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Engine:       [cyan]{repo_path}[/cyan]")
    console.print(f"  Version:      [green]{version}[/green]")
    console.print(f"  Dependencies: [green]{len(dependencies)}[/green] direct")
    console.print(f"  Studio root:  [cyan]{studio_root}[/cyan]")
    console.print(f"  Packages dir: [cyan]{packages_root}[/cyan]")
    overwrite = (packages_root / "griptape_nodes_engine" / version).exists()
    if overwrite:
        console.print(f"  [yellow]Existing package {version} will be overwritten.[/yellow]")
    console.print()

    _warn_if_rez_does_not_search(packages_root)
    _check_rez_bindings(interactive=not yes)

    if not yes and not typer.confirm("Proceed with build?", default=True):
        raise typer.Abort

    packages_root.mkdir(parents=True, exist_ok=True)
    _install_deps(dependencies, packages_root.parent, skip_installed=skip_installed)
    _write_engine_package(repo_path, packages_root.parent, version, dependencies)
    _validate_rez_package("griptape_nodes_engine", version)
    _print_env_var_guidance(studio_root, rez_paths, cross_platform_roots=cross_platform_roots)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _admin_path(value: str) -> Path:
    r"""Canonicalize a path the administrator typed, in the form they will see and configure.

    These paths are echoed back in the summary and the GTN_REZ_* guidance, so they must
    not carry the Windows ``\\?\`` prefix that ``canonicalize_for_io`` always adds there.
    ``canonicalize_for_identity`` does not fit either: it follows symlinks, which can
    rewrite a mapped studio drive such as ``P:`` to its UNC share.
    """
    return Path(strip_windows_long_path_prefix(canonicalize_for_io(value)))


def _resolve_and_validate_repo(engine_repo_path: str | None) -> tuple[Path, Path]:
    """Locate and validate the engine repo. Prompts interactively if not found."""
    console.print(Panel("[bold cyan]Rez Engine Package Builder[/bold cyan]", expand=False))
    console.print()

    repo_path = _resolve_engine_repo(engine_repo_path)

    if repo_path is None:
        console.print("[yellow]Could not auto-detect the engine repository.[/yellow]")
        console.print("The engine repo contains pyproject.toml and src/griptape_nodes/.")
        console.print()
        user_path = typer.prompt("Enter the path to your griptape-nodes-engine checkout")
        repo_path = _admin_path(user_path)
        if not repo_path.is_dir():
            console.print(f"[red]Directory not found: {repo_path}[/red]")
            raise typer.Exit(1)

    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.exists():
        console.print(f"[red]No pyproject.toml found at {pyproject_path}[/red]")
        raise typer.Exit(1)

    console.print(f"  Engine repo: [cyan]{repo_path}[/cyan]")
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
            "on your studio configuration with REZ_CONFIG_FILE:\n\n"
            f'  [cyan]packages_path = ModifyList(append=["{store.as_posix()}"])[/cyan]\n\n'
            "Griptape Nodes never changes your rez configuration.",
            title="Package Store Not On Rez Search Path",
            expand=False,
        )
    )


def _check_rez_bindings(*, interactive: bool = True) -> None:
    """Verify that rez system bindings (platform, os, python) exist.

    These packages are created by ``rez bind`` and are required for rez
    to resolve environments on this machine. Without them, built packages
    will fail to resolve when someone tries to use them.

    When *interactive* is True (default), prompts the user to continue.
    When False (--yes mode), prints a warning but proceeds.
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
        console.print("[yellow]Proceeding without bindings (--yes mode).[/yellow]")
        console.print()
        return

    if not typer.confirm("Continue building without rez bindings?", default=False):
        raise typer.Abort

    console.print()


def _read_project_metadata(pyproject_path: Path) -> tuple[str, list[str]]:
    """Read version and dependencies from pyproject.toml."""
    version = _read_version(pyproject_path)
    if version is None:
        console.print("[red]Could not read version from pyproject.toml[/red]")
        raise typer.Exit(1)

    dependencies = _read_dependencies(pyproject_path)
    console.print(f"  Version:      [green]{version}[/green]")
    console.print(f"  Dependencies: [green]{len(dependencies)}[/green]")
    console.print()
    return version, dependencies


_DEFAULT_REZ_PATHS: dict[str, str] = {
    "bin": "rez/bin",
    "config_file": "rez/rezconfig.py",
    "local_packages": "rez/packages/local",
    "release_packages": "rez/packages/release",
}


def _resolve_studio_root_from_env_or_option(packages_path: str | None) -> tuple[Path, dict[str, str]]:
    """Non-interactive fallback: derive studio root from env or CLI option."""
    if packages_path:
        packages_root = _admin_path(packages_path)
        return packages_root, {**_DEFAULT_REZ_PATHS, "local_packages": "local"}

    root = rez_root()
    if root:
        return root, dict(_DEFAULT_REZ_PATHS)

    console.print("[red]Set GTN_REZ_ROOT or provide --packages-path for non-interactive mode.[/red]")
    raise typer.Exit(1)


def _prompt_studio_setup() -> tuple[dict[str, str] | None, Path, dict[str, str]]:
    """Interactive wizard: uses existing env vars if set, prompts only if missing.

    Returns ``(cross_platform_roots, studio_root, rez_paths)`` where:
    - cross_platform_roots is the platform mapping dict (or None for single-platform)
    - studio_root is the resolved root Path for this platform
    - rez_paths is a dict of relative paths for downstream vars
    """
    existing_root = rez_root()
    existing_map = rez_path_map()

    key_to_env = {
        "bin": "GTN_REZ_BIN_PATH",
        "config_file": "GTN_REZ_CONFIG_FILE",
        "local_packages": "GTN_REZ_LOCAL_PACKAGES_PATH",
        "release_packages": "GTN_REZ_RELEASE_PACKAGES_PATH",
    }

    if existing_root:
        console.print(f"  Using GTN_REZ_ROOT: [cyan]{existing_root}[/cyan]")
        rez_paths: dict[str, str] = {}
        for key, default in _DEFAULT_REZ_PATHS.items():
            env_val = os.environ.get(key_to_env.get(key, ""), "").strip()
            rez_paths[key] = env_val or default
        return existing_map or None, existing_root, rez_paths

    console.print()
    is_cross = typer.confirm("Is this a cross-platform studio deployment?", default=False)

    if is_cross:
        console.print()
        console.print("[bold]Enter the studio root path for each platform (leave blank to skip):[/bold]")
        roots: dict[str, str] = {}
        prompts = [
            ("linux", "  Linux path   (e.g. /mnt/media/pipeline)"),
            ("windows", "  Windows path (e.g. P:)"),
            ("osx", "  macOS path   (e.g. /Volumes/pipeline)"),
        ]
        for key, prompt in prompts:
            val = typer.prompt(prompt, default="", show_default=False).strip()
            if val:
                roots[key] = val

        if not roots:
            console.print("[yellow]No platform roots provided.[/yellow]")
            raise typer.Exit(1)

        platform = current_platform_key()
        if platform not in roots:
            console.print(f"[yellow]Current platform '{platform}' not in mapping — using first entry.[/yellow]")
            studio_root = Path(next(iter(roots.values())))
        else:
            studio_root = Path(roots[platform])

        cross_platform_roots = roots
    else:
        console.print()
        root_str = typer.prompt("Enter the studio root path (GTN_REZ_ROOT)")
        studio_root = _admin_path(root_str)
        cross_platform_roots = None

    console.print()
    console.print(f"  Studio root: [cyan]{studio_root}[/cyan]")

    rez_paths = _prompt_rez_paths()
    return cross_platform_roots, studio_root, rez_paths


def _prompt_rez_paths() -> dict[str, str]:
    """Prompt for rez path configuration, supporting absolute paths for local packages."""
    console.print()
    console.print("[bold]Rez paths (relative to GTN_REZ_ROOT, or absolute for local storage):[/bold]")
    rez_paths: dict[str, str] = {}
    for key, default in _DEFAULT_REZ_PATHS.items():
        label = key.replace("_", " ")
        if key == "local_packages":
            use_absolute = typer.confirm(
                "  Use an absolute path for local packages? (e.g. user-local build directory)",
                default=False,
            )
            if use_absolute:
                val = typer.prompt("  local packages (absolute path)").strip()
                val = str(_admin_path(val))
            else:
                val = typer.prompt(f"  {label}", default=default).strip()
        else:
            val = typer.prompt(f"  {label}", default=default).strip()
        rez_paths[key] = val
    return rez_paths


def _install_deps(dependencies: list[str], packages_root: Path, *, skip_installed: bool) -> None:
    """Install pip dependencies as individual rez packages."""
    if not dependencies:
        console.print("[yellow]No pip dependencies found — skipping dependency install.[/yellow]")
        return

    console.print("[bold]Installing dependencies as rez packages...[/bold]")
    try:
        rez_uv_install(
            dependencies,
            packages_dir=packages_root,
            skip_installed=skip_installed,
        )
    except subprocess.CalledProcessError as exc:
        console.print("[red]Attempted to install the engine's dependencies as rez packages. Failed due to:[/red]")
        _print_uv_error(exc)
        raise typer.Exit(1) from exc
    except OSError as exc:
        console.print(
            f"[red]Attempted to install the engine's dependencies as rez packages. Failed due to: {exc}[/red]"
        )
        raise typer.Exit(1) from exc

    console.print("[green]Dependencies installed.[/green]")
    console.print()


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
    packages_root: Path,
    version: str,
    dependencies: list[str],
) -> None:
    """Write the engine meta-package with source, .dist-info, and requires."""
    console.print("[bold]Building engine meta-package...[/bold]")
    family = "griptape_nodes_engine"
    local_dir = packages_root / "local"
    version_dir = local_dir / family / version

    if version_dir.exists():
        console.print(f"  [yellow]Package already exists at {version_dir} — overwriting.[/yellow]")
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
# Generated by: gtn rez build-engine-package
# Source: {repo_path}
# Built: {timestamp}

name = '{family}'

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
            f"  Package: [cyan]{family}-{version}[/cyan]\n"
            f"  Location: [cyan]{version_dir}[/cyan]\n"
            f"  Dependencies: [cyan]{len(requires)}[/cyan] direct requires",
            title="Success",
            expand=False,
        )
    )
    _print_next_steps(version)


def _print_next_steps(version: str) -> None:
    """Print verification and next-step commands."""
    console.print()
    table = Table(title="Next Steps", show_header=False, show_edge=False, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Verify:", "rez-search griptape_nodes_engine")
    table.add_row("Test:", f'rez-env griptape_nodes_engine-{version} -- python -c "import griptape_nodes"')
    table.add_row("Launch:", "Use your project/launch package (e.g. rez-env griptape_launch -- gtn engine)")
    console.print(table)


def _format_path_map(roots: dict[str, str]) -> str:
    """Serialize a platform roots dict to GTN_REZ_PATH_MAP format."""
    return ";".join(f"{k}={v}" for k, v in sorted(roots.items()))


def _print_env_var_guidance(
    studio_root: Path,
    rez_paths: dict[str, str],
    *,
    cross_platform_roots: dict[str, str] | None = None,
) -> None:
    """Print the environment variables needed for a launch/project package."""
    console.print()
    console.print(Panel("[bold]Environment Variables for Launch Package[/bold]", expand=False))
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Variable", style="cyan")
    table.add_column("Value", style="green")
    table.add_column("Purpose")
    if cross_platform_roots:
        table.add_row("GTN_REZ_PATH_MAP", _format_path_map(cross_platform_roots), "Cross-platform root mapping")
    table.add_row("GTN_REZ_ROOT", str(studio_root), "Studio root for this platform")
    table.add_row("GTN_REZ_BIN_PATH", rez_paths["bin"], "Rez binary dir (relative to root)")
    table.add_row("GTN_REZ_CONFIG_FILE", rez_paths["config_file"], "Rez configuration")
    table.add_row("GTN_REZ_LOCAL_PACKAGES_PATH", rez_paths["local_packages"], "Local packages")
    table.add_row("GTN_REZ_RELEASE_PACKAGES_PATH", rez_paths["release_packages"], "Release packages")
    console.print(table)

    console.print()
    console.print("Example package.py commands() block:")
    console.print()

    if cross_platform_roots:
        roots_repr = repr(cross_platform_roots)
        path_map_str = _format_path_map(cross_platform_roots)
        path_lines = "\n".join(
            f"    env.GTN_REZ_{k.upper()} = '{v}'" for k, v in sorted(rez_paths.items()) if k != "config_file"
        )
        console.print(f"""[dim]def commands():
    _roots = {roots_repr}
    env.GTN_REZ_PATH_MAP = '{path_map_str}'
    env.GTN_REZ_ROOT = _roots.get(system.platform, '')
{path_lines}
    env.GTN_REZ_CONFIG_FILE = '{rez_paths["config_file"]}'[/dim]""")
    else:
        path_lines = "\n".join(
            f"    env.GTN_REZ_{k.upper()} = '{v}'" for k, v in sorted(rez_paths.items()) if k != "config_file"
        )
        console.print(f"""[dim]def commands():
    env.GTN_REZ_ROOT = '{studio_root}'
{path_lines}
    env.GTN_REZ_CONFIG_FILE = '{rez_paths["config_file"]}'[/dim]""")

    console.print()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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

    console.print("[red]Could not auto-detect engine repo.[/red]")
    console.print("Provide it explicitly: gtn rez build-engine-package --engine-repo /path/to/engine")
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
def build_library_package(
    git_url: str = typer.Option(None, "--git-url", help="Git URL to clone and build."),
    branch: str = typer.Option(None, "--branch", help="Branch, tag, or commit to checkout."),
    local_path: str = typer.Option(None, "--local-path", help="Local path to library directory."),
    packages_path: str = typer.Option(
        None,
        "--packages-path",
        help=(
            "Rez package store root; packages are written to <path>/local. "
            "Defaults to the parent of GTN_REZ_LOCAL_PACKAGES_PATH."
        ),
    ),
    skip_installed: bool = typer.Option(  # noqa: FBT001
        True,
        "--skip-installed/--reinstall",
        help="Skip dependencies whose rez package already exists.",
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

    if packages_path:
        packages_root = _admin_path(packages_path)
    else:
        local_path_resolved = rez_local_packages_path()
        if local_path_resolved:
            packages_root = local_path_resolved.parent
        else:
            console.print("[red]Set GTN_REZ_ROOT + GTN_REZ_LOCAL_PACKAGES_PATH or provide --packages-path.[/red]")
            raise typer.Exit(1)

    _warn_if_rez_does_not_search(packages_root / "local")
    _check_rez_bindings()

    if git_url:
        _build_library_from_git(git_url, branch, packages_root, skip_installed=skip_installed)
    else:
        _build_library_from_local(local_path, packages_root, skip_installed=skip_installed)  # type: ignore[arg-type]


def _build_library_from_git(
    git_url: str,
    branch: str | None,
    packages_root: Path,
    *,
    skip_installed: bool,
) -> None:
    """Clone a git repo and build a rez package from its library manifest."""
    console.print(Panel("[bold cyan]Rez Library Package Builder (git)[/bold cyan]", expand=False))
    console.print()
    console.print(f"  Git URL: [cyan]{git_url}[/cyan]")
    if branch:
        console.print(f"  Branch:  [cyan]{branch}[/cyan]")
    console.print()

    temp_dir = Path(tempfile.mkdtemp(prefix="rez_lib_build_"))
    clone_target = temp_dir / "repo"

    try:
        console.print("[bold]Cloning repository...[/bold]")
        clone_repository(git_url, clone_target, branch)
        console.print("[green]Clone complete.[/green]")
        console.print()

        _build_library_from_dir(clone_target, packages_root, skip_installed=skip_installed)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_library_from_local(
    local_path: str,
    packages_root: Path,
    *,
    skip_installed: bool,
) -> None:
    """Build a rez package from a local library directory."""
    library_dir = _admin_path(local_path)
    if not library_dir.is_dir():
        console.print(f"[red]Directory not found: {library_dir}[/red]")
        raise typer.Exit(1)

    console.print(Panel("[bold cyan]Rez Library Package Builder (local)[/bold cyan]", expand=False))
    console.print()
    console.print(f"  Library dir: [cyan]{library_dir}[/cyan]")
    console.print()

    _build_library_from_dir(library_dir, packages_root, skip_installed=skip_installed)


def _build_library_from_dir(  # noqa: C901
    library_dir: Path, packages_root: Path, *, skip_installed: bool
) -> None:
    """Build a rez package from a directory containing a library manifest.

    Also recursively builds rez packages for any declared library dependencies.
    """
    library_json = find_library_manifest(library_dir)
    if library_json is None:
        console.print(f"[red]No library manifest found in {library_dir}[/red]")
        raise typer.Exit(1)

    console.print(f"  Manifest: [cyan]{library_json}[/cyan]")

    library_name, pip_dependencies, pip_dependencies_exec, pip_install_flags = read_library_manifest(library_json)
    if not library_name:
        console.print("[red]Could not read library name from manifest.[/red]")
        raise typer.Exit(1)

    library_deps = read_library_dependencies(library_json)

    console.print(f"  Library:      [green]{library_name}[/green]")
    console.print(f"  Pip deps:     [green]{len(pip_dependencies)} edit + {len(pip_dependencies_exec)} exec[/green]")
    if pip_install_flags:
        console.print(f"  Install flags: [green]{' '.join(pip_install_flags)}[/green]")
    if library_deps:
        console.print(f"  Library deps: [green]{len(library_deps)}[/green]")
    console.print()

    # Recursively build rez packages for library dependencies first
    if library_deps:
        console.print("[bold]Building library dependencies...[/bold]")
        for lib_dep in library_deps:
            dep_url = lib_dep["url"]
            console.print(f"  Dependency: [cyan]{dep_url}[/cyan]")
            try:
                _build_library_from_git(str(dep_url), None, packages_root, skip_installed=True)
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
        install_library_as_rez_package(
            library_name,
            pip_dependencies,
            pip_dependencies_exec=pip_dependencies_exec or None,
            library_file_path=library_json,
            pip_install_flags=pip_install_flags or None,
            skip_installed=skip_installed,
            packages_root=packages_root,
        )
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Attempted to build a rez package for '{library_name}'. Failed due to:[/red]")
        _print_uv_error(exc)
        raise typer.Exit(1) from exc
    except OSError as exc:
        console.print(f"[red]Attempted to build a rez package for '{library_name}'. Failed due to: {exc}[/red]")
        raise typer.Exit(1) from exc

    family = library_file_path_to_rez_family(library_json)
    version = get_library_rez_package_version(library_json, packages_root=packages_root)

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
            console.print(f"  [yellow]rez-search {family}: not found (rez may need PACKAGES_PATH configured)[/yellow]")
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
    console.print(table)
