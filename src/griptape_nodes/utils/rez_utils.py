"""Utilities for Rez environment integration."""

import ast
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.utils.file_utils import find_file_in_directory
from griptape_nodes.utils.git_utils import get_git_repository_root
from griptape_nodes.utils.rez_uv import (
    FALLBACK_PLATFORM_KEY,
    TORCH_BACKEND_EPHEMERAL,
    TORCH_BACKENDS,
    InstallReport,
    ParsedRequirement,
    flags_for_torch_backend,
    marker_applies,
    merge_platform_requires,
    parse_requirement,
    pip_normalize,
    platform_environments,
    read_package_file,
    resolve_full,
    rez_name,
    rez_range,
    torch_backend_request,
)
from griptape_nodes.utils.rez_uv import current_platform_key as rez_uv_platform_key
from griptape_nodes.utils.rez_uv import install as rez_uv_install

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables (documentation only — all values are read at runtime
# via the accessor functions below, never cached as module-level values)
# ---------------------------------------------------------------------------
#
# GTN_REZ_BIN_PATH       — Folder holding rez's tools (rez, rez-env, ...) for this
#                          platform. Absolute, or relative to the base below. Rez
#                          integration is on exactly when this resolves to a folder
#                          containing rez-env.
#
# GTN_REZ_ROOT           — Optional base for relative GTN_REZ_* paths on this machine.
#
# GTN_REZ_PATH_MAP       — Optional per-platform bases, used when GTN_REZ_ROOT is unset.
#                          Format: linux=/mnt/pipeline;osx=/Volumes/pipeline;windows=P:
#                          Platform keys match rez's system.platform: linux, osx, windows.
#
# GTN_REZ_LOCAL_PACKAGES_PATH — Optional. The package store builds write to. Set it to
#                          build and test packages; unset means production, where every
#                          package comes from the studio's released packages.
#
# GTN_REZ_CONFIG_FILE    — Optional rezconfig appended after the studio's REZ_CONFIG_FILE
#                          (or used alone when there is none). It must extend the studio
#                          configuration, never replace it.
#
# GTN_REZ_TORCH_BACKEND  — Optional, per machine: the torch build this workstation uses
#                          (cu118, cu128, cpu, ...) when detection picks the wrong one.
#                          Never set it in a launch package shared by every workstation.

ENV_BIN_PATH = "GTN_REZ_BIN_PATH"
ENV_ROOT = "GTN_REZ_ROOT"
ENV_PATH_MAP = "GTN_REZ_PATH_MAP"
ENV_LOCAL_PACKAGES_PATH = "GTN_REZ_LOCAL_PACKAGES_PATH"
ENV_CONFIG_FILE = "GTN_REZ_CONFIG_FILE"
ENV_TORCH_BACKEND = "GTN_REZ_TORCH_BACKEND"

# Every variable an administrator sets to configure rez integration.
REZ_CONFIG_ENV_VARS: tuple[str, ...] = (
    ENV_BIN_PATH,
    ENV_ROOT,
    ENV_PATH_MAP,
    ENV_LOCAL_PACKAGES_PATH,
    ENV_CONFIG_FILE,
    ENV_TORCH_BACKEND,
)

# ---------------------------------------------------------------------------
# Platform mapping
# ---------------------------------------------------------------------------

_PLATFORM_MAP = {"linux": "linux", "darwin": "osx", "win32": "windows"}


def current_platform_key() -> str:
    """Map ``sys.platform`` to rez's platform key (linux/osx/windows)."""
    return _PLATFORM_MAP.get(sys.platform, "linux")


def rez_platform_key() -> str:
    """This machine as rez's ``<platform>-<arch>`` (``osx-arm64``), the key of ``platform_requires``."""
    return rez_uv_platform_key()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RezBase:
    """The folder relative GTN_REZ_* paths are joined to, and the variable that supplied it."""

    path: Path
    source: str


@dataclass(frozen=True)
class RezSetup:
    """Rez configuration as the engine and the CLI both read it.

    ``disabled_reason`` explains why rez is off when the administrator configured it and
    something is wrong. It is empty when rez is on, and when rez is simply not configured.
    ``warnings`` lists configuration that was ignored.
    """

    enabled: bool
    disabled_reason: str
    bin_path: Path | None
    base: RezBase | None
    local_packages_path: Path | None
    config_file: Path | None
    warnings: tuple[str, ...]


def _env_value(name: str) -> str:
    return os.getenv(name, "").strip()


def is_rez_enabled() -> bool:
    """Return True when rez integration is active.

    Rez is on when ``GTN_REZ_BIN_PATH`` resolves to a folder containing ``rez-env``.
    See ``rez_setup`` for the reason when it is off.
    """
    return rez_setup().enabled


def rez_setup() -> RezSetup:
    """Read the rez configuration from the environment.

    Evaluated once per distinct set of ``GTN_REZ_*`` values, so repeated calls cost
    nothing and a changed environment (tests, the CLI) is picked up.
    """
    return _rez_setup_for(tuple(os.getenv(name, "") for name in REZ_CONFIG_ENV_VARS))


@functools.cache
def _rez_setup_for(env_values: tuple[str, ...]) -> RezSetup:  # noqa: ARG001 (the cache key)
    base = rez_base()
    platform_key = current_platform_key()
    warnings: list[str] = []

    optional_paths: dict[str, Path | None] = {}
    for name in (ENV_LOCAL_PACKAGES_PATH, ENV_CONFIG_FILE):
        resolved = _resolve_rez_path(name)
        if _env_value(name) and resolved is None:
            warnings.append(
                f"{name} is the relative path '{_env_value(name)}', and neither {ENV_ROOT} nor a "
                f"{ENV_PATH_MAP} entry for this platform ({platform_key}) says what it is relative to. "
                "It is ignored."
            )
        optional_paths[name] = resolved

    raw_bin = _env_value(ENV_BIN_PATH)
    bin_path = _resolve_rez_path(ENV_BIN_PATH)

    if not raw_bin:
        configured = [name for name in REZ_CONFIG_ENV_VARS if name != ENV_BIN_PATH and _env_value(name)]
        reason = ""
        if configured:
            verb = "is" if len(configured) == 1 else "are"
            reason = (
                f"Rez is not active: {', '.join(configured)} {verb} set, but {ENV_BIN_PATH} is not. "
                f"Set {ENV_BIN_PATH} to the folder that holds rez's tools (rez-env)."
            )
        enabled = False
    elif bin_path is None:
        reason = (
            f"Rez is not active: {ENV_BIN_PATH} is the relative path '{raw_bin}', and neither {ENV_ROOT} "
            f"nor a {ENV_PATH_MAP} entry for this platform ({platform_key}) says what it is relative to."
        )
        enabled = False
    elif _find_executable(bin_path, "rez-env") is None:
        reason = (
            f"Rez is not active: no rez-env was found in {bin_path} ({ENV_BIN_PATH}). "
            "Point it at the folder that holds rez's tools."
        )
        enabled = False
    else:
        reason = ""
        enabled = True

    return RezSetup(
        enabled=enabled,
        disabled_reason=reason,
        bin_path=bin_path,
        base=base,
        local_packages_path=optional_paths[ENV_LOCAL_PACKAGES_PATH],
        config_file=optional_paths[ENV_CONFIG_FILE],
        warnings=tuple(warnings),
    )


def rez_path_map() -> dict[str, str]:
    """Parse ``GTN_REZ_PATH_MAP`` into a ``{platform: root_path}`` dict.

    Format: ``linux=/mnt/pipeline;osx=/Volumes/pipeline;windows=P:``
    Returns an empty dict when the env var is unset.
    """
    raw = os.getenv(ENV_PATH_MAP, "")
    if not raw:
        return {}
    result: dict[str, str] = {}
    for raw_entry in raw.split(";"):
        stripped = raw_entry.strip()
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def rez_base() -> RezBase | None:
    """Return the base for relative ``GTN_REZ_*`` paths, or None when nothing supplies one.

    ``GTN_REZ_ROOT`` wins; otherwise this platform's ``GTN_REZ_PATH_MAP`` entry.
    """
    root = _env_value(ENV_ROOT)
    mapped = rez_path_map().get(current_platform_key(), "")
    if root:
        if mapped and Path(_anchor_drive_letter(mapped)) != Path(_anchor_drive_letter(root)):
            logger.debug("[Rez] %s (%s) differs from %s (%s); using %s", ENV_ROOT, root, ENV_PATH_MAP, mapped, ENV_ROOT)
        return RezBase(path=Path(_anchor_drive_letter(root)), source=ENV_ROOT)
    if mapped:
        return RezBase(path=Path(_anchor_drive_letter(mapped)), source=ENV_PATH_MAP)
    return None


def _anchor_drive_letter(value: str) -> str:
    """Anchor a drive-relative Windows path such as ``P:`` to the drive root (``P:/``).

    Studio roots are commonly mapped drive letters (``windows=P:``). On Windows a
    bare ``P:`` means "the current directory on drive P", so joining it with
    ``rez/packages`` yields ``P:rez/packages`` instead of ``P:/rez/packages``.
    """
    match = re.fullmatch(r"([A-Za-z]:)(?![\\/])(.*)", value)
    if match is None:
        return value
    drive, rest = match.groups()
    return f"{drive}/{rest}"


def _resolve_rez_path(env_var: str) -> Path | None:
    """Resolve a ``GTN_REZ_*`` path env var.

    Absolute paths are returned directly. Relative paths are joined with the base
    (``GTN_REZ_ROOT``, or this platform's ``GTN_REZ_PATH_MAP`` entry). Returns None
    when the variable is unset, or relative with no base to join it to.
    """
    val = _env_value(env_var)
    if not val:
        return None
    path = Path(_anchor_drive_letter(val))
    if path.is_absolute():
        return path
    base = rez_base()
    if base is None:
        return None
    return base.path / val


def rez_bin_path() -> Path | None:
    """Return the folder holding rez's tools, from ``GTN_REZ_BIN_PATH``."""
    return _resolve_rez_path(ENV_BIN_PATH)


def rez_config_file() -> Path | None:
    """Return Griptape Nodes' own rezconfig, from ``GTN_REZ_CONFIG_FILE``."""
    return _resolve_rez_path(ENV_CONFIG_FILE)


def rez_local_packages_path() -> Path | None:
    """Return the package store builds write to, from ``GTN_REZ_LOCAL_PACKAGES_PATH``."""
    return _resolve_rez_path(ENV_LOCAL_PACKAGES_PATH)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _executable_candidate_names(command: str) -> list[str]:
    """Return possible executable filenames for *command* on the current platform.

    On POSIX this is just ``[command]``. On Windows, ``shutil.which``'s
    ``path=`` override does not reliably apply ``PATHEXT`` suffixes on Python
    versions before 3.12, so we enumerate the candidates ourselves (e.g.
    ``rez.exe``, ``rez.cmd``) unless *command* already ends with one.
    """
    if os.name != "nt":
        return [command]
    pathext = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if ext]
    if any(command.lower().endswith(ext.lower()) for ext in pathext):
        return [command]
    return [command + ext for ext in pathext] + [command]


def _find_executable(bin_dir: Path, command: str) -> Path | None:
    """Return *command* inside *bin_dir* (handling Windows suffixes), or None."""
    for candidate_name in _executable_candidate_names(command):
        candidate_path = bin_dir / candidate_name
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return candidate_path
    return None


def _rez_executable(command: str) -> str:
    """Return the path to a rez CLI command in ``GTN_REZ_BIN_PATH``.

    Never searches ``PATH``: another rez found there may not be the one the studio
    configured. When the tool is missing the returned path does not exist, so the
    caller's subprocess fails with the path in its error.
    """
    bin_dir = rez_bin_path()
    if bin_dir is None:
        return command
    found = _find_executable(bin_dir, command)
    if found is not None:
        return str(found)
    return str(bin_dir / command)


def rez_subprocess_env() -> dict[str, str]:
    """Return the environment for rez commands run by Griptape Nodes.

    The process environment is inherited unchanged, so a studio's ``REZ_CONFIG_FILE``
    and ``REZ_PACKAGES_PATH`` are used as-is. ``GTN_REZ_CONFIG_FILE`` is the explicit
    opt-in: when set, it is layered after any existing ``REZ_CONFIG_FILE`` so its
    settings apply on top of the studio configuration instead of replacing it.
    """
    env = dict(os.environ)
    config_file = rez_config_file()
    if config_file is None:
        return env

    entries = [entry for entry in env.get("REZ_CONFIG_FILE", "").split(os.pathsep) if entry]
    if str(config_file) not in entries:
        entries.append(str(config_file))
    env["REZ_CONFIG_FILE"] = os.pathsep.join(entries)
    return env


def rez_search_paths(env: dict[str, str] | None = None) -> list[Path] | None:
    """Return the package search path rez is configured with, or None if rez cannot say.

    Read-only: runs ``rez-config --json packages_path``, by default in the same
    environment as every other rez command Griptape Nodes runs.
    """
    cmd = [_rez_executable("rez-config"), "--json", "packages_path"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            env=env if env is not None else rez_subprocess_env(),
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[Rez] could not read rez packages_path: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug("[Rez] rez-config packages_path failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return None

    try:
        paths = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.debug("[Rez] unexpected rez-config output: %s", result.stdout.strip())
        return None
    return [Path(path) for path in paths]


def rez_package_stores() -> list[Path]:
    """Return the directories rez resolves packages from, in rez's search order.

    Package lookups (``REZ:`` entries, library package versions) read these, so the
    engine finds exactly what ``rez env`` can resolve: the local store when rez is
    configured to search it, and the studio's released packages. Read once per rez
    configuration; empty when rez is off or cannot report its search path.
    """
    if not is_rez_enabled():
        return []
    rez_env = rez_subprocess_env()
    key = (str(rez_bin_path()), rez_env.get("REZ_CONFIG_FILE", ""), rez_env.get("REZ_PACKAGES_PATH", ""))
    return list(_package_stores_for(key))


@functools.cache
def _package_stores_for(key: tuple[str, str, str]) -> tuple[Path, ...]:  # noqa: ARG001 (the cache key)
    paths = rez_search_paths()
    if paths is None:
        logger.warning("[Rez] Attempted to read rez's package search path. Failed, so no rez packages can be found.")
        return ()
    return tuple(paths)


def rez_unsearched_stores(stores: list[Path] | None = None) -> list[Path]:
    """Return the package stores that rez's ``packages_path`` does not include.

    Packages in such a store are written correctly but cannot be resolved by
    ``rez env``. Defaults to the local store (``GTN_REZ_LOCAL_PACKAGES_PATH``) when set.
    Returns an empty list when rez cannot report its search path.
    """
    if stores is None:
        local = rez_local_packages_path()
        stores = [local] if local is not None else []
    if not stores:
        return []

    search_paths = rez_search_paths()
    if search_paths is None:
        return []

    searched = {canonicalize_for_identity(path) for path in search_paths}
    return [store for store in stores if canonicalize_for_identity(store) not in searched]


def rez_config_dropped_paths() -> list[Path]:
    """Return studio search paths that ``GTN_REZ_CONFIG_FILE`` removes instead of extending.

    Read-only: compares rez's ``packages_path`` without and with Griptape Nodes' rezconfig.
    A rezconfig that assigns ``packages_path = [...]`` replaces the studio's list; one that
    uses ``ModifyList(append=[...])`` extends it. Empty when there is nothing to compare.
    """
    if rez_config_file() is None:
        return []
    studio_paths = rez_search_paths(env=dict(os.environ))
    layered_paths = rez_search_paths()
    if studio_paths is None or layered_paths is None:
        return []
    kept = {canonicalize_for_identity(path) for path in layered_paths}
    return [path for path in studio_paths if canonicalize_for_identity(path) not in kept]


def _log_rez_env_context() -> None:
    """Emit debug lines describing the current Rez configuration."""
    setup = rez_setup()
    logger.debug("[Rez] enabled=%s", setup.enabled)
    if setup.disabled_reason:
        logger.debug("[Rez] %s", setup.disabled_reason)
    if setup.base is not None:
        logger.debug("[Rez] base=%s (from %s)", setup.base.path, setup.base.source)
    logger.debug("[Rez] bin_path=%s (raw=%s)", setup.bin_path or "(unset)", _env_value(ENV_BIN_PATH) or "(unset)")
    logger.debug("[Rez] config_file=%s", setup.config_file or "(none)")
    logger.debug("[Rez] local_packages_path=%s", setup.local_packages_path or "(none: production)")


# ---------------------------------------------------------------------------
# Library installation helpers
# ---------------------------------------------------------------------------


def _version_sort_key(p: Path) -> tuple[int, ...]:
    """Parse a version directory name into a numerically-sortable key.

    Splits on '.' and converts each component to int so that '1.10.0'
    sorts after '1.9.0' (unlike lexicographic string sort). Non-numeric
    components (pre-release suffixes) get -1 so they sort before their
    numeric counterpart.
    """
    result = []
    for part in p.name.split("."):
        try:
            result.append(int(part))
        except ValueError:
            result.append(-1)
    return tuple(result)


def _find_package_version_dir(rez_family: str, version: str | None, stores: list[Path]) -> Path | None:
    """Return the version directory of *rez_family* across *stores*, or None.

    A pinned *version* comes from the first store that holds it. Otherwise the highest
    version across all stores wins, as in a rez resolve; on a tie the earlier store wins.
    """
    if version is not None:
        for store in stores:
            version_dir = store / rez_family / version
            if (version_dir / "package.py").is_file():
                return version_dir
        return None

    candidates: list[Path] = []
    for store in stores:
        family_dir = store / rez_family
        if not family_dir.is_dir():
            continue
        candidates.extend(d for d in family_dir.iterdir() if d.is_dir() and (d / "package.py").is_file())
    if not candidates:
        return None
    return max(candidates, key=_version_sort_key)


def pip_spec_name(spec: str) -> str:
    """Extract the bare package name from a pip requirement spec, PEP 503 normalized.

    ``'torch>=2.0,<3'`` → ``'torch'``
    ``'diffusers[torch]==0.39.0'`` → ``'diffusers'``
    ``'ruamel_yaml>=0.18'`` → ``'ruamel-yaml'``
    """
    name = re.split(r"[>=<!~\[\s;]", spec)[0].strip()
    return re.sub(r"[-._]+", "-", name).lower()


def find_library_manifest(directory: Path) -> Path | None:
    """Find a Griptape library manifest JSON in a directory tree.

    Matches both naming conventions used across library repos:
    ``griptape_nodes_library.json`` (underscore) and
    ``griptape-nodes-library.json`` (hyphen).
    """
    return find_file_in_directory(directory, "griptape[-_]nodes[-_]library.json")


def read_library_manifest(library_json: Path) -> tuple[str, list[str], list[str], list[str]]:
    """Read library name, pip dependencies (split), and install flags from a manifest.

    Returns ``(name, pip_dependencies, pip_dependencies_exec, pip_install_flags)``:

    - ``pip_dependencies`` — edit-time only (what the orchestrator needs)
    - ``pip_dependencies_exec`` — execution-time only (what the worker needs
      on top of edit-time deps)
    - ``pip_install_flags`` — shared flags for uv/pip

    Returns ``("", [], [], [])`` if the file cannot be read.
    """
    try:
        with library_json.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("[Rez] failed to read library manifest: %s", library_json)
        return "", [], [], []

    name = data.get("name", "")
    pip_dependencies: list[str] = []
    pip_dependencies_exec: list[str] = []
    pip_install_flags: list[str] = []

    metadata = data.get("metadata", {})
    deps = metadata.get("dependencies", {})
    if deps:
        pip_dependencies = list(deps.get("pip_dependencies", []) or [])
        pip_dependencies_exec = list(deps.get("pip_dependencies_exec", []) or [])
        pip_install_flags = list(deps.get("pip_install_flags", []) or [])

    return name, pip_dependencies, pip_dependencies_exec, pip_install_flags


def read_library_dependencies(library_json: Path) -> list[dict[str, str | bool]]:
    """Read library dependency declarations from a manifest JSON file.

    Returns a list of dicts with ``url`` and ``required`` for each
    ``library_dependency`` declaration. These are other Griptape node
    libraries that this library depends on (e.g. OpenEXR depends on
    OpenColorIO).
    """
    try:
        with library_json.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    declarations = data.get("metadata", {}).get("declarations", [])
    return [
        {"url": d["url"], "required": d.get("required", False)}
        for d in declarations
        if isinstance(d, dict) and d.get("type") == "library_dependency" and "url" in d
    ]


def build_direct_requires(pip_dependencies: list[str]) -> list[str]:
    """Resolve pip dependencies and return pinned rez requires for direct deps only.

    Resolves the full transitive dependency tree with uv, then filters to
    only the packages that were directly requested (by name). Each is pinned
    to its resolved version in rez naming format.
    """
    if not pip_dependencies:
        return []

    resolved = resolve_full(pip_dependencies)
    direct_names = {pip_spec_name(spec) for spec in pip_dependencies}
    return [
        f"{rez_name(pkg.pip_name)}-{pkg.version}" for pkg in resolved if pip_spec_name(pkg.pip_name) in direct_names
    ]


# ---------------------------------------------------------------------------
# Rez execution helpers
# ---------------------------------------------------------------------------


REZ_PATH_PREFIX = "REZ:"


def is_rez_library_path(path: str) -> bool:
    """Return True when *path* uses the ``REZ:<family>`` syntax."""
    return path.startswith(REZ_PATH_PREFIX)


def _parse_rez_spec(spec: str) -> tuple[str, str | None]:
    """Split a rez package spec into (family, version_or_none).

    Rez uses ``family-version`` notation. The family name itself may contain
    underscores but never hyphens (normalised away), so the *last* hyphen is
    the separator when a version is present. A bare family with no hyphen
    returns ``(family, None)``.

    Examples::

        "griptape_nodes_library_standard-0.81.0"  → ("griptape_nodes_library_standard", "0.81.0")
        "griptape_nodes_library_standard"          → ("griptape_nodes_library_standard", None)
    """
    idx = spec.rfind("-")
    if idx <= 0:
        return spec, None
    candidate_version = spec[idx + 1 :]
    if candidate_version and candidate_version[0].isdigit():
        return spec[:idx], candidate_version
    return spec, None


def rez_version_from_git_ref(ref: str | None) -> str | None:
    """Read a library dependency's ``@ref`` as a rez version, or None if it is not one.

    Library packages are versioned from ``pyproject.toml`` / release tags, so a tag
    such as ``1.2.0`` or ``v1.2.0`` names a package version. Branches and commit
    hashes do not, and only dotted numeric refs are accepted so a hash like
    ``3f9c2e1`` is not mistaken for one.
    """
    if not ref:
        return None
    candidate = ref[1:] if ref[:1] in ("v", "V") else ref
    if re.match(r"^\d+(\.\d+)+", candidate) is None:
        return None
    return candidate


def rez_library_package_name(path: str) -> str:
    """Extract the rez family name from a ``REZ:<family>[-<version>]`` path string.

    Handles both ``REZ:family`` and ``REZ:family-version`` formats. When a
    version is present (e.g. ``REZ:griptape_nodes_library_standard-0.81.0``),
    only the family portion is returned.
    """
    raw = path[len(REZ_PATH_PREFIX) :]
    family, version = _parse_rez_spec(raw)
    if version:
        logger.debug("[Rez] parsed REZ: spec '%s' → family='%s', version='%s'", raw, family, version)
    return family


def rez_library_package_version(path: str) -> str | None:
    """Extract the version from a ``REZ:<family>-<version>`` path string, or None."""
    raw = path[len(REZ_PATH_PREFIX) :]
    _, version = _parse_rez_spec(raw)
    return version


def resolve_rez_library_json_path(rez_family: str, version: str | None = None) -> Path | None:
    """Locate the library JSON file inside a rez package's ``python/`` directory.

    Searches the directories on rez's package search path (see ``rez_package_stores``).
    When *version* is provided, looks for exactly that version; otherwise uses the
    highest version found, as a rez resolve would.

    Args:
        rez_family: Rez package family name (no version).
        version: Optional specific version to resolve (e.g. "0.81.0").
    """
    stores = rez_package_stores()
    if not stores:
        return None

    target_dir = _find_package_version_dir(rez_family, version or None, stores)
    if target_dir is None:
        searched = ", ".join(str(store) for store in stores)
        if version:
            logger.warning("[Rez] package '%s-%s' not found in %s", rez_family, version, searched)
        else:
            logger.warning("[Rez] package family '%s' not found in %s", rez_family, searched)
        return None

    python_dir = target_dir / "python"
    if not python_dir.is_dir():
        logger.warning("[Rez] no python/ directory in %s", target_dir)
        return None

    for candidate in python_dir.iterdir():
        if candidate.is_file() and candidate.suffix == ".json" and "library" in candidate.name.lower():
            logger.debug("[Rez] resolved REZ:%s → %s", rez_family, candidate)
            return candidate

    logger.warning("[Rez] no library JSON found in %s", python_dir)
    return None


def is_library_rez_package_available(library_file_path: Path) -> bool:
    """Check whether a rez meta-package already exists for a library.

    Looks for any version directory with a ``package.py`` under the library's
    rez family (see ``library_file_path_to_rez_family``) on rez's package search path.
    """
    return get_library_rez_package_version(library_file_path) is not None


def get_library_rez_package_version(library_file_path: Path, *, store: Path | None = None) -> str | None:
    """Return the latest version of a library's rez package, or None if unavailable.

    Searches rez's package search path for version directories containing a
    ``package.py`` and returns the highest version found.

    Args:
        library_file_path: Library JSON path. The rez family is derived from the
            library's repo or folder name (see ``library_file_path_to_rez_family``).
        store: A package store being built, searched instead of rez's search path.
            Used by the build CLI.
    """
    if store is None:
        stores = rez_package_stores()
    else:
        stores = [store]
    if not stores:
        return None

    rez_family = library_file_path_to_rez_family(library_file_path)
    version_dir = _find_package_version_dir(rez_family, None, stores)
    if version_dir is None:
        return None

    version = version_dir.name
    logger.debug("[Rez] found rez package for '%s': %s-%s", library_file_path, rez_family, version)
    return version


@dataclass(frozen=True)
class LibraryRezFamily:
    """A library's rez family name and what it was derived from (for administrators)."""

    family: str
    source: str


def library_file_path_to_rez_family(library_file_path: Path) -> str:
    """Derive the rez package family name from the library's on-disk directory.

    See ``library_rez_family`` for the resolution order.
    """
    return library_rez_family(library_file_path).family


def library_rez_family(library_file_path: Path) -> LibraryRezFamily:
    """Derive a library's rez family name, and say where it came from.

    Resolution order:
    1. Rez package store path — if the manifest is inside a rez package
       (``<store>/<family>/<version>/python/<manifest>``), extract
       the family directly from the path structure.
    2. Git remote origin URL — parse the repo name from the remote. Handles
       tempdir clones where the local directory name is meaningless.
    3. Git repository root name — handles subdirectory layouts where the JSON
       lives inside a nested folder (e.g. ``my-library/luma/library.json``
       resolves from the ``my-library`` git root, not ``luma``).
    4. Parent directory name — fallback for non-git libraries.

    All names are normalised: hyphens, dots, and spaces become underscores,
    lowercased.
    """
    library_dir = library_file_path.parent

    # Check if the manifest lives inside a rez package store:
    # <store>/<family>/<version>/python/<manifest.json>
    store_family = _detect_rez_store_family(library_file_path)
    if store_family:
        logger.debug(
            "[Rez] naming: rez store path → family '%s' (json at %s)",
            store_family,
            library_file_path,
        )
        return LibraryRezFamily(family=store_family, source="its rez package folder")

    try:
        git_root = get_git_repository_root(library_dir)
        if git_root is not None:
            remote_name = _git_remote_repo_name(library_dir)
            if remote_name:
                family = _library_rez_name(remote_name)
                logger.debug(
                    "[Rez] naming: git remote '%s' → family '%s' (json at %s)",
                    remote_name,
                    family,
                    library_file_path,
                )
                return LibraryRezFamily(family=family, source=f"git remote repository '{remote_name}'")

            family = _library_rez_name(git_root.name)
            logger.debug(
                "[Rez] naming: git root '%s' → family '%s' (json at %s)",
                git_root.name,
                family,
                library_file_path,
            )
            return LibraryRezFamily(family=family, source=f"git repository folder '{git_root.name}'")
    except (OSError, subprocess.SubprocessError):
        logger.debug("[Rez] naming: git lookup failed for %s, falling back to parent dir", library_dir)

    family = _library_rez_name(library_dir.name)
    logger.debug("[Rez] naming: parent dir '%s' → family '%s' (no git root)", library_dir.name, family)
    return LibraryRezFamily(family=family, source=f"folder '{library_dir.name}'")


def _git_remote_repo_name(directory: Path) -> str | None:
    """Parse the repository name from the git remote origin URL.

    ``https://github.com/org/my-repo.git`` → ``my-repo``
    ``git@github.com:org/my-repo.git``     → ``my-repo``
    """
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=directory,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    url = result.stdout.strip()
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = name.removesuffix(".git")
    return name or None


def _detect_rez_store_family(library_file_path: Path) -> str | None:
    """Detect if a manifest lives inside a rez package store and extract the family.

    Rez store layout: ``<store>/<family>/<version>/python/<manifest.json>``
    The ``python/`` parent is the giveaway — check if the grandparent has a
    ``package.py`` (rez version dir marker).
    """
    min_rez_store_depth = 4
    parts = library_file_path.parts
    if len(parts) < min_rez_store_depth:
        return None
    parent_name = parts[-2]
    if parent_name != "python":
        return None
    # version dir is parts[-3], should contain package.py
    version_dir = library_file_path.parent.parent
    if not (version_dir / "package.py").exists():
        return None
    # family dir is parts[-4]
    family = parts[-4]
    return family


def _library_package_dir(library_file_path: Path) -> Path | None:
    """Return the version directory of the rez package that provides a library, or None.

    A manifest registered from the store (``REZ:`` entries) sits inside that directory
    already; a manifest in a local checkout is matched to the highest version of its
    repo/folder-named family on rez's package search path.
    """
    if _detect_rez_store_family(library_file_path) is not None:
        return library_file_path.parent.parent

    rez_family = library_file_path_to_rez_family(library_file_path)
    return _find_package_version_dir(rez_family, None, rez_package_stores())


def read_library_package_requires(library_file_path: Path) -> list[str]:
    """Return the ``requires`` list of the rez package that provides a library.

    The library meta-package pins every direct dependency (``torch-2.7.0``), so these
    are the versions a worker's ``rez env <family>`` resolves. Returns an empty list
    when the package or its ``requires`` cannot be read.
    """
    package_dir = _library_package_dir(library_file_path)
    if package_dir is None:
        return []

    package_file = package_dir / "package.py"
    platform_requires = read_package_file(package_file).platform_requires
    if platform_requires is not None:
        # Requires that differ by platform: this machine's list, as rez would pick it.
        return platform_requires.get(rez_platform_key(), platform_requires.get(FALLBACK_PLATFORM_KEY, []))

    return _literal_requires(package_file)


def _literal_requires(package_file: Path) -> list[str]:
    """The literal ``requires = [...]`` list of a package.py, read without executing it."""
    try:
        tree = ast.parse(package_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        logger.debug("[Rez] could not read requires from %s", package_file, exc_info=True)
        return []

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "requires" for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            logger.debug("[Rez] requires in %s is not a literal list", package_file)
            return []
        if not isinstance(value, list):
            return []
        return [str(entry) for entry in value]

    return []


def library_edit_rez_requests(library_file_path: Path, edit_dependencies: list[str]) -> list[str]:
    """Build rez requests for a library's edit-time dependencies, pinned as its package pins them.

    Resolving bare names would let rez pick the newest version in the store, which
    can differ from the version the library package (and so the worker) uses. A
    dependency missing from the package's ``requires`` falls back to its bare name.
    """
    pinned: dict[str, str] = {}
    for requirement in read_library_package_requires(library_file_path):
        family, version = _parse_rez_spec(requirement)
        if version is not None:
            pinned[family] = requirement

    requests: list[str] = []
    for dependency in edit_dependencies:
        family = rez_name(pip_spec_name(dependency))
        request = pinned.get(family)
        if request is None:
            logger.debug("[Rez] no pinned version for '%s' in %s's package — using latest", family, library_file_path)
            request = family
        requests.append(request)
    return requests


def is_in_rez_context(rez_family: str) -> bool:
    """Return True when this process runs inside a rez context that resolved *rez_family*.

    ``rez env`` records the resolved packages as ``family-version`` tokens in
    ``REZ_USED_RESOLVE``. A library's worker runs inside ``rez env <family>``, so its
    dependencies are already on PYTHONPATH in the versions rez chose.
    """
    for token in os.environ.get("REZ_USED_RESOLVE", "").split():
        family, _ = _parse_rez_spec(token)
        if family == rez_family:
            return True
    return False


def build_rez_env_prefix(package_specs: list[str]) -> list[str]:
    """Build the ``rez env <specs> --`` command prefix for subprocess wrapping.

    Args:
        package_specs: Rez package request strings (e.g. ``["griptape_nodes_library_diffusers"]``).

    Returns:
        Command prefix list that can be prepended to any subprocess args list.
        Example: ``["/opt/rez/bin/rez", "env", "griptape_nodes_library_diffusers", "--"]``
    """
    rez_bin = _rez_executable("rez")
    prefix = [rez_bin, "env", *package_specs, "--"]
    logger.info("[Rez][execution] built rez-env prefix: %s", " ".join(prefix))
    return prefix


def resolve_and_log_rez_context(package_specs: list[str]) -> list[str]:
    """Probe a rez context via the binary and log each resolved package.

    Uses ``rez env <specs> -- python -c "..."`` to read ``REZ_USED_RESOLVE``
    from inside the resolved environment.  Binary-only — does not import the
    rez Python API so it works regardless of whether rez is installed as a
    Python package.

    Args:
        package_specs: Rez package request strings.

    Returns:
        List of resolved ``"name-version"`` strings, or empty list on failure.
    """
    _log_rez_env_context()
    logger.info("[Rez][execution] resolving context for specs: %s", package_specs)
    rez_bin = _rez_executable("rez")
    probe_cmd = [
        rez_bin,
        "env",
        *package_specs,
        "--",
        "python",
        "-c",
        "import os; [print(p) for p in os.environ.get('REZ_USED_RESOLVE', '').split()]",
    ]
    logger.debug("[Rez][execution] probe cmd: %s", " ".join(probe_cmd))
    try:
        result = subprocess.run(  # noqa: S603
            probe_cmd,
            capture_output=True,
            text=True,
            env=rez_subprocess_env(),
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("[Rez][execution] rez probe failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return []
        resolved = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for pkg in resolved:
            logger.info("[Rez][execution]   resolved: %s", pkg)
        logger.info("[Rez][execution] total resolved packages: %d", len(resolved))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[Rez][execution] rez probe error: %s", exc)
        return []
    else:
        return resolved


def resolve_rez_pythonpath(package_specs: list[str]) -> list[str]:
    """Resolve a rez environment and return the PYTHONPATH entries it provides.

    Runs ``rez env <specs> -- python -c "..."`` to read the ``PYTHONPATH``
    from inside the resolved environment. Used by the orchestrator to add
    rez-provided dependency paths to ``sys.path`` for in-process library loading.

    Returns a list of directory paths, or empty list on failure.
    """
    rez_bin = _rez_executable("rez")
    probe_cmd = [
        rez_bin,
        "env",
        *package_specs,
        "--",
        "python",
        "-c",
        "import os; [print(p) for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]",
    ]
    logger.debug("[Rez] resolving PYTHONPATH for specs: %s", package_specs)
    try:
        result = subprocess.run(  # noqa: S603
            probe_cmd,
            capture_output=True,
            text=True,
            env=rez_subprocess_env(),
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("[Rez] PYTHONPATH resolve failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return []
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for p in paths:
            logger.debug("[Rez]   PYTHONPATH entry: %s", p)
        logger.info("[Rez] resolved %d PYTHONPATH entries for %s", len(paths), package_specs)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[Rez] PYTHONPATH resolve error: %s", exc)
        return []
    else:
        return paths


def _library_rez_name(library_name: str) -> str:
    """Normalise a repo or folder name to a valid rez family name.

    Library packages are named after the repo or folder they were built from
    (see ``library_file_path_to_rez_family``), never the manifest's display name.
    Unlike the pip-oriented ``rez_name()`` in rez_uv, this also collapses spaces,
    which folder names may contain.
    """
    return re.sub(r"[-._\s]+", "_", library_name).lower()


def _read_pyproject_version(pyproject_path: Path) -> str | None:
    """Parse a version string from a pyproject.toml file.

    Checks ``[project] version`` (PEP 517/518) then ``[tool.poetry] version``.
    Returns None when the file cannot be read or contains no version.
    """
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)

        version = data.get("project", {}).get("version")
        if version:
            return str(version)

        version = data.get("tool", {}).get("poetry", {}).get("version")
        if version:
            return str(version)

    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Unable to read version from %s", pyproject_path, exc_info=True)

    return None


def _git_describe_version(directory: Path) -> str | None:
    """Return a version string derived from ``git describe --tags`` in *directory*.

    Output format: ``X.Y.Z`` for exact tag matches, ``X.Y.Z.postN`` when N
    commits ahead of the tag.  Returns None when git is unavailable or the
    directory has no tags.
    """
    git_cmd = ["git", "describe", "--tags", "--long"]
    result = subprocess.run(  # noqa: S603
        git_cmd,
        capture_output=True,
        text=True,
        cwd=directory,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    _git_describe_parts = 3
    raw = result.stdout.strip().lstrip("v")
    parts = raw.rsplit("-", 2)
    if len(parts) == _git_describe_parts:
        tag, count, _hash = parts
        tag = tag.lstrip("v")
        return tag if count == "0" else f"{tag}.post{count}"
    return raw


def derive_library_version(library_file_path: Path) -> str:
    """Derive a version string for a library, searching in priority order.

    1. ``pyproject.toml`` in the library directory or up to four parent dirs.
    2. ``git describe --tags`` in the library directory.
    3. Fallback: ``"1.0.0"``.
    """
    search_dir = library_file_path.parent
    for _ in range(4):
        pyproject = search_dir / "pyproject.toml"
        if pyproject.exists():
            version = _read_pyproject_version(pyproject)
            if version:
                logger.debug("[Rez] library version from %s: %s", pyproject, version)
                return version
        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent

    git_version = _git_describe_version(library_file_path.parent)
    if git_version:
        logger.debug("[Rez] library version from git describe: %s", git_version)
        return git_version

    logger.debug("[Rez] library version not found — using 1.0.0")
    return "1.0.0"


REZ_PACKAGE_COPY_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".venv",
    ".venv-exec",
    ".git",
    ".git*",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    "node_modules",
    ".tox",
    "dist",
    "*.egg-info",
    "build",
)

_LIBRARY_COPY_EXCLUDES = shutil.ignore_patterns(*REZ_PACKAGE_COPY_EXCLUDE_PATTERNS)


def library_platform_requires(
    pip_dependencies: list[str], resolved_versions: dict[str, str]
) -> dict[str, list[str]] | None:
    """A library package's requires per platform, or None when no dependency depends on the platform.

    Dependencies are pinned to the versions this build resolved. One marked for other
    platforms only (``bitsandbytes; sys_platform == 'win32'`` built on macOS) was not
    resolved here, so its manifest range is used until that platform builds the library.
    """
    parsed = [requirement for spec in pip_dependencies if (requirement := parse_requirement(spec)) is not None]
    if not any(requirement.depends_on_platform for requirement in parsed):
        return None

    this_key = rez_platform_key()
    common: list[str] = []
    platform_specific: list[ParsedRequirement] = []
    for requirement in parsed:
        if requirement.depends_on_platform:
            platform_specific.append(requirement)
            continue
        pinned = _pinned_library_request(requirement, resolved_versions)
        if pinned is not None:
            common.append(pinned)

    per_platform: dict[str, list[str]] = {FALLBACK_PLATFORM_KEY: sorted(common)}
    for key, environment in platform_environments().items():
        extras: list[str] = []
        for requirement in platform_specific:
            if not marker_applies(requirement.marker, environment):
                continue
            pinned = _pinned_library_request(requirement, resolved_versions) if key == this_key else None
            extras.append(pinned or rez_range(requirement))
        per_platform[key] = sorted(common + extras)
    return per_platform


def _pinned_library_request(requirement: ParsedRequirement, resolved_versions: dict[str, str]) -> str | None:
    """``family-version`` for a dependency this build resolved, or None."""
    version = resolved_versions.get(pip_normalize(requirement.pip_name))
    if version is None:
        return None
    return f"{rez_name(requirement.pip_name)}-{version}"


def _write_library_meta_package(  # noqa: PLR0913
    library_name: str,
    library_version: str,
    resolved_requires: list[str],
    store: Path,
    *,
    rez_family: str,
    library_source_dir: Path,
    library_json_name: str,
    skip_installed: bool = True,
    platform_requires: dict[str, list[str]] | None = None,
) -> None:
    """Write a rez meta-package that bundles the library source alongside its dep declarations.

    The library source is copied into ``<version_dir>/python/`` (excluding
    ``.venv``, ``.git``, ``__pycache__``, etc.) so the rez package is
    self-contained. The generated ``commands()`` block appends ``{root}/python``
    to ``PYTHONPATH``, making the library's nodes importable in any rez
    environment that resolves this package.

    When the library's dependencies differ by platform, the package carries a
    ``platform_requires`` dictionary. A build on another platform adds its own entry to an
    existing package instead of skipping it, so each platform's pins come from its own build.

    Args:
        library_name: Human-readable library name (e.g. "Luma Labs Library").
        library_version: Version string for the meta-package (e.g. "1.0.0").
        resolved_requires: Pinned rez requires strings (e.g. ["torch-2.7.0", ...]).
        store: The package store to write into.
        rez_family: Rez family name, derived from the library's repo or folder name.
        library_source_dir: Directory containing the library's Python source
            (parent of the griptape_nodes_library.json file). Copied into the
            rez package so the package is self-contained and portable.
        library_json_name: Filename of the library manifest JSON (e.g.
            ``"griptape_nodes_library.json"``). Set as ``GTN_REZ_LIBRARY_JSON``
            in the generated ``commands()`` block.
        skip_installed: When True, keep an existing package's source (and, for requires
            that do not differ by platform, the whole package). When False, overwrite.
        platform_requires: Requires per platform key (see ``library_platform_requires``),
            merged with an existing package's entries.
    """
    version_dir = store / rez_family / library_version
    version_dir.mkdir(parents=True, exist_ok=True)

    pkg_file = version_dir / "package.py"
    exists = pkg_file.exists()
    if exists and skip_installed and platform_requires is None:
        logger.debug("[Rez] meta-package already exists for library '%s' — skipping write", library_name)
        return
    if platform_requires is not None:
        platform_requires = merge_platform_requires(read_package_file(pkg_file).platform_requires, platform_requires)

    escaped_name = library_name.replace("'", "\\'")

    python_dest = version_dir / "python"
    if not exists or not skip_installed:
        logger.info("[Rez] copying library source into rez package: %s → %s", library_source_dir, python_dest)
        shutil.copytree(
            library_source_dir,
            python_dest,
            ignore=_LIBRARY_COPY_EXCLUDES,
            dirs_exist_ok=True,
        )
    else:
        logger.info("[Rez] adding this platform's requires to existing library package %s", version_dir)

    if platform_requires is not None:
        entries = "".join(f"    {key!r}: {sorted(value)!r},\n" for key, value in sorted(platform_requires.items()))
        requires_block = f"""# Dependencies differ by platform; rez picks this machine's list when it resolves.
platform_requires = {{
{entries}}}


@late()
def requires():
    key = system.platform + '-' + system.arch
    return this.platform_requires.get(key, this.platform_requires.get('{FALLBACK_PLATFORM_KEY}', []))
"""
    else:
        req_entries = "".join(f"    '{r}',\n" for r in sorted(resolved_requires))
        requires_block = f"""requires = [
{req_entries}]
"""

    escaped_json_name = library_json_name.replace("'", "\\'")
    commands_block = f"""

def commands():
    env.PYTHONPATH.append('{{root}}/python')
    env.GTN_REZ_LIBRARY_JSON = '{{root}}/python/{escaped_json_name}'
"""

    build_timestamp = datetime.now(UTC).isoformat()
    source_dir_str = str(library_source_dir) if library_source_dir else "(none)"
    content = f"""\
# -*- coding: utf-8 -*-
# Generated by griptape_nodes — do not edit by hand.
# Rez meta-package for the '{escaped_name}' Griptape node library.
#
# Source directory: {source_dir_str}
# Library JSON:    {library_json_name}
# Built:           {build_timestamp}

name = '{rez_family}'

version = '{library_version}'

description = 'Griptape Nodes library: {escaped_name}'

{requires_block}
format_version = 2
{commands_block}"""
    pkg_file.write_text(content, encoding="utf-8")
    logger.info(
        "[Rez] wrote meta-package '%s-%s' with %d requires", rez_family, library_version, len(resolved_requires)
    )


@dataclass
class LibraryInstallResult:
    """What building a library package installed."""

    report: InstallReport
    built_torch_backends: list[str]
    skipped_torch_backends: list[str]


def install_library_as_rez_package(  # noqa: C901, PLR0912, PLR0913
    library_name: str,
    pip_dependencies: list[str],
    *,
    library_file_path: Path,
    pip_dependencies_exec: list[str] | None = None,
    extra_index_url: str | None = None,
    pip_install_flags: list[str] | None = None,
    python_version: str | None = None,
    skip_installed: bool = True,
    store: Path | None = None,
    torch_backends: list[str] | None = None,
) -> LibraryInstallResult | None:
    """Install a library's pip dependencies as rez packages and write a library meta-package.

    All dependencies (edit + exec) are installed as individual rez packages so
    the worker's ``rez-env <family> --`` can resolve everything.  The library
    meta-package's ``requires`` list includes ALL deps so the worker inherits
    the full set.  The orchestrator resolves only the edit-time subset when
    populating ``sys.path`` — see ``_add_library_paths_to_sys_path``.

    Args:
        library_name: Human-readable library name (used to derive the rez family name).
        pip_dependencies: Edit-time pip dependency specs from the library JSON.
        pip_dependencies_exec: Execution-time pip dependency specs.  Installed as
            rez packages alongside edit deps but kept separate so the orchestrator
            can resolve only what it needs.
        library_file_path: Path to the library JSON file. The package is named
            after the library's repo or folder, its source is copied from this
            file's directory, and ``pyproject.toml`` / git metadata next to it
            supply the version.
        extra_index_url: Additional pip index URL (e.g. a PyTorch CUDA mirror).
        pip_install_flags: Extra flags for uv resolution and install (e.g.
            ``["--torch-backend=auto"]``). Read from the library manifest's
            ``pip_install_flags`` field.
        python_version: Python version string (``"3.12"``). Defaults to current interpreter.
        skip_installed: When True (default), skip packages that already have a variant
            this machine uses. When False, reinstall this machine's variants.
        store: The package store to write into. Defaults to
            ``GTN_REZ_LOCAL_PACKAGES_PATH``.
        torch_backends: Torch builds to install (``["cu118", "cu128"]``), each as its own
            variant. Replaces the manifest's torch source. ``["all"]`` tries every build uv
            knows and skips those that do not publish the pinned torch version. None uses
            the manifest's flags as they are.

    Returns:
        What was installed, or None when no store is configured.

    Raises:
        RezInstallError: When any dependency could not be installed. The library
            meta-package is not written, so nothing references missing packages.
        subprocess.CalledProcessError: When uv cannot resolve the dependencies (for every
            requested torch build, when ``torch_backends`` is used).
    """
    if store is None:
        store = rez_local_packages_path()
    if store is None:
        logger.warning(
            "[Rez] GTN_REZ_LOCAL_PACKAGES_PATH is not set — cannot install library '%s' as a rez package",
            library_name,
        )
        return None

    all_pip_dependencies = list(pip_dependencies)
    if pip_dependencies_exec:
        all_pip_dependencies.extend(pip_dependencies_exec)

    library_version = derive_library_version(library_file_path)

    skip_unavailable = torch_backends == ["all"]
    if skip_unavailable:
        torch_backends = list(TORCH_BACKENDS)
    # One install pass per torch build; None means the manifest's flags as they are.
    passes: list[tuple[str | None, list[str] | None]] = [(None, pip_install_flags)]
    if torch_backends:
        passes = [(backend, flags_for_torch_backend(pip_install_flags, backend)) for backend in torch_backends]

    result = LibraryInstallResult(report=InstallReport(), built_torch_backends=[], skipped_torch_backends=[])

    # A library without pip dependencies still needs its meta-package: it carries the
    # library source and manifest, which is what makes the library registrable via REZ:.
    resolved_requires: list[str] = []
    platform_requires: dict[str, list[str]] | None = None
    if all_pip_dependencies:
        logger.info(
            "[Rez] resolving deps for library '%s' (%d edit + %d exec specs) ...",
            library_name,
            len(pip_dependencies),
            len(pip_dependencies_exec or []),
        )

        direct_names = {pip_spec_name(spec) for spec in all_pip_dependencies}
        for backend, flags in passes:
            try:
                resolved = resolve_full(
                    all_pip_dependencies,
                    extra_index_url=extra_index_url,
                    extra_flags=flags,
                    python_version=python_version,
                )
            except subprocess.CalledProcessError:
                if backend is None or not skip_unavailable:
                    raise
                logger.info("[Rez] torch build '%s' is not available for this library's pins — skipped", backend)
                result.skipped_torch_backends.append(backend)
                continue

            if not resolved_requires:
                # The library's pins are the same for every torch build (plain versions).
                resolved_requires = [
                    f"{rez_name(pkg.pip_name)}-{pkg.version}"
                    for pkg in resolved
                    if pip_spec_name(pkg.pip_name) in direct_names
                ]
                resolved_versions = {pip_normalize(pkg.pip_name): pkg.version for pkg in resolved}
                platform_requires = library_platform_requires(all_pip_dependencies, resolved_versions)

            report = rez_uv_install(
                all_pip_dependencies,
                packages_dir=store,
                extra_index_url=extra_index_url,
                extra_flags=flags,
                python_version=python_version,
                skip_installed=skip_installed,
            )
            result.report.merge(report)
            if backend is not None:
                result.built_torch_backends.append(backend)

        if torch_backends and not result.built_torch_backends:
            msg = (
                f"Attempted to build library '{library_name}' for torch builds {', '.join(torch_backends)}. "
                "Failed because none of them publish the torch version the library pins."
            )
            raise RuntimeError(msg)
    else:
        logger.info("[Rez] library '%s' has no pip dependencies — writing its package only", library_name)

    # Name the package after the repo / folder it was built from (e.g.
    # griptape_nodes_library_diffusers), not the manifest's display name, so a rez
    # admin can match installed packages to their sources.
    _write_library_meta_package(
        library_name,
        library_version,
        resolved_requires,
        store,
        rez_family=library_file_path_to_rez_family(library_file_path),
        library_source_dir=library_file_path.parent,
        library_json_name=library_file_path.name,
        skip_installed=skip_installed,
        platform_requires=platform_requires,
    )
    return result


# ---------------------------------------------------------------------------
# torch builds (torch_backend) at load and execution time
# ---------------------------------------------------------------------------

# Stands in for a torch build the engine could not determine. No variant requires it, so any
# library whose resolve needs a torch build fails to resolve, while every other library is
# unaffected: a library that needs a torch build this workstation cannot be matched to does
# not load.
TORCH_BACKEND_UNKNOWN = "unknown"

_CUDA_BACKEND = re.compile(r"^cu(\d+)(\d)$")
_STUDIO_TORCH_BACKEND = re.compile(rf"^~?\.{TORCH_BACKEND_EPHEMERAL}(?:==|-)(\S+)$")
_DRIVER_CUDA = re.compile(r"CUDA Version:\s*(\d+)\.(\d+)")


@dataclass(frozen=True)
class TorchBackendChoice:
    """The torch build this workstation uses, and why.

    ``backend`` is None when no torch build needs choosing (no library package in the store
    was built per torch build, e.g. on macOS). ``studio_managed`` means the studio's rez
    configuration already requests one, so the engine adds nothing.
    """

    backend: str | None
    reason: str
    studio_managed: bool = False


def choose_torch_backend() -> TorchBackendChoice:
    """Choose the torch build for this workstation, once per rez configuration.

    In order: the studio's implicit ``.torch_backend`` (rez ``implicit_packages``), then
    ``GTN_REZ_TORCH_BACKEND`` on this machine, then detection: the highest torch build in the
    store that this machine's NVIDIA driver supports, or the CPU build without an NVIDIA GPU.
    When nothing fits, ``TORCH_BACKEND_UNKNOWN``.
    """
    if not is_rez_enabled():
        return TorchBackendChoice(backend=None, reason="rez is not active")
    key = (
        os.getenv(ENV_TORCH_BACKEND, "").strip(),
        os.getenv("REZ_USED_IMPLICIT_PACKAGES", ""),
        tuple(str(store) for store in rez_package_stores()),
    )
    return _choose_torch_backend_for(key)


@functools.cache
def _choose_torch_backend_for(key: tuple[str, str, tuple[str, ...]]) -> TorchBackendChoice:
    override, _, store_names = key

    studio_backend = _studio_torch_backend()
    if studio_backend is not None:
        return TorchBackendChoice(
            backend=studio_backend,
            reason="set by your studio's rez configuration (implicit_packages)",
            studio_managed=True,
        )
    if override:
        return TorchBackendChoice(backend=override, reason=f"set by {ENV_TORCH_BACKEND} on this machine")

    built = built_torch_backends([Path(name) for name in store_names])
    if not built:
        return TorchBackendChoice(backend=None, reason="no torch package in the store was built per torch build")
    return _detect_torch_backend(built)


def _detect_torch_backend(built: list[str]) -> TorchBackendChoice:
    """Pick from the *built* torch builds for this machine's NVIDIA driver (or its lack of one)."""
    built_text = ", ".join(built)
    driver = nvidia_driver_cuda_version()
    if driver is None:
        if "cpu" in built:
            return TorchBackendChoice(backend="cpu", reason=f"no NVIDIA GPU found; built: {built_text}")
        return TorchBackendChoice(
            backend=TORCH_BACKEND_UNKNOWN,
            reason=f"no NVIDIA GPU was found and no CPU build of torch is in the store (built: {built_text})",
        )

    driver_text = f"{driver[0]}.{driver[1]}"
    supported = [backend for backend in built if (cuda := cuda_version_of(backend)) is not None and cuda <= driver]
    if not supported:
        return TorchBackendChoice(
            backend=TORCH_BACKEND_UNKNOWN,
            reason=f"this workstation's NVIDIA driver supports CUDA {driver_text}, older than every CUDA build "
            f"of torch in the store (built: {built_text})",
        )
    best = max(supported, key=lambda backend: cuda_version_of(backend) or (0, 0))
    return TorchBackendChoice(backend=best, reason=f"driver supports CUDA {driver_text}; built: {built_text}")


def torch_backend_requests() -> list[str]:
    """Rez requests that pick this workstation's torch build: ``[".torch_backend-cu128"]`` or none.

    Added to every resolve the engine runs for a library. Harmless for libraries without
    torch; empty when the studio already requests one or none needs choosing.
    """
    choice = choose_torch_backend()
    if choice.studio_managed or choice.backend is None:
        return []
    return [torch_backend_request(choice.backend)]


def cuda_version_of(backend: str) -> tuple[int, int] | None:
    """The CUDA version a torch build targets: ``cu128`` → (12, 8), ``cu130`` → (13, 0)."""
    match = _CUDA_BACKEND.match(backend)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def built_torch_backends(stores: list[Path]) -> list[str]:
    """The torch builds installed for this platform in *stores*, oldest CUDA first."""
    rez_platform, _, rez_arch = rez_platform_key().partition("-")
    found: set[str] = set()
    for store in stores:
        family_dir = store / "torch"
        if not family_dir.is_dir():
            continue
        for package_file in family_dir.glob("*/package.py"):
            for variant in read_package_file(package_file).variants:
                if f"platform-{rez_platform}" not in variant or f"arch-{rez_arch}" not in variant:
                    continue
                prefix = f".{TORCH_BACKEND_EPHEMERAL}-"
                found.update(entry.removeprefix(prefix) for entry in variant if entry.startswith(prefix))
    return sorted(found, key=lambda backend: cuda_version_of(backend) or (0, 0))


def nvidia_driver_cuda_version() -> tuple[int, int] | None:
    """The highest CUDA version this machine's NVIDIA driver supports, from ``nvidia-smi``, or None."""
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [nvidia_smi], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[Rez] nvidia-smi failed: %s", exc)
        return None
    match = _DRIVER_CUDA.search(result.stdout)
    if result.returncode != 0 or match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _studio_torch_backend() -> str | None:
    """The torch build the studio's rez configuration requests on every resolve, if any."""
    implicit = os.getenv("REZ_USED_IMPLICIT_PACKAGES")
    if implicit is not None:
        entries = implicit.split()
    else:
        entries = rez_implicit_packages()
    for entry in entries:
        match = _STUDIO_TORCH_BACKEND.match(entry)
        if match is not None:
            return match.group(1)
    return None


def rez_implicit_packages() -> list[str]:
    """Rez's ``implicit_packages`` setting (read-only), or an empty list if rez cannot say."""
    cmd = [_rez_executable("rez-config"), "--json", "implicit_packages"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[Rez] could not read rez implicit_packages: %s", exc)
        return []
    if result.returncode != 0:
        return []
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [str(entry) for entry in entries] if isinstance(entries, list) else []


def rez_resolve_failure(package_specs: list[str]) -> str | None:
    """Resolve *package_specs* without starting a shell; return why it fails, or None if it resolves.

    Used at load time to check a library's worker environment, so a library whose worker
    could never start is reported when it loads, not when a node first runs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [_rez_executable("rez"), "env", *package_specs, "--output", str(Path(tmp) / "context.rxt")]
        try:
            result = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=120
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"rez could not be run: {exc}"
    if result.returncode == 0:
        return None
    return _resolve_failure_summary(result.stderr or result.stdout)


def _resolve_failure_summary(output: str) -> str:
    """The line rez prints after "The context failed to resolve:", or the output's last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("The context failed to resolve") and index + 1 < len(lines):
            return lines[index + 1]
    if lines:
        return lines[-1]
    return "rez could not resolve it"


def library_environment_failure(library_file_path: Path) -> str | None:
    """Why a library's worker environment cannot resolve on this workstation, or None if it can.

    Resolves ``rez env <family>`` with this workstation's torch build, the same request its
    worker makes. The reason is written for artists.
    """
    family = library_file_path_to_rez_family(library_file_path)
    requests = torch_backend_requests()
    failure = rez_resolve_failure([family, *requests])
    if failure is None:
        return None
    choice = choose_torch_backend()
    if choice.backend == TORCH_BACKEND_UNKNOWN:
        return (
            f"this workstation's GPU build of torch could not be determined ({choice.reason}). "
            "Ask your rez administrator to build a matching torch build or set .torch_backend for this machine."
        )
    return f"its rez environment '{family}' does not resolve on this workstation: {failure}"


# ---------------------------------------------------------------------------
# Health check operations
# ---------------------------------------------------------------------------


def check_rez_health() -> bool:
    """Verify rez is reachable and list the available package families.

    Runs ``rez-search --type family`` using the configured binary, in the same
    environment as every other rez command (see ``rez_subprocess_env``).  At INFO level logs the package count; at DEBUG logs
    every family name found.  Returns True when rez responds successfully,
    False when the binary is missing or exits non-zero (warning is emitted).

    Intended to be called once at engine startup when rez is enabled,
    so misconfiguration surfaces immediately in the log rather than
    failing silently later.
    """
    rez_search = _rez_executable("rez-search")
    cmd = [rez_search, "--type", "family"]

    env = rez_subprocess_env()

    logger.info("[Rez] health check: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)  # noqa: S603

    if result.returncode != 0:
        logger.warning(
            "[Rez] health check failed (exit %d) — rez may not be installed or GTN_REZ_BIN_PATH may be wrong",
            result.returncode,
        )
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-5:]:
                logger.info("[Rez]   stderr: %s", line)
        return False

    families = [line for line in result.stdout.splitlines() if line.strip()]
    logger.info("[Rez] operational — %d package families visible", len(families))
    if logger.isEnabledFor(logging.DEBUG):
        for family in sorted(families):
            logger.debug("[Rez]   %s", family)

    return True


@dataclass
class RezHealthResult:
    """Structured result from a rez health check with timing information."""

    healthy: bool
    check_duration_ms: float
    package_count: int
    timestamp: str


def check_rez_health_detailed() -> RezHealthResult:
    """Run a rez health check and return structured timing data."""
    start = time.monotonic()

    rez_search = _rez_executable("rez-search")
    cmd = [rez_search, "--type", "family"]

    env = rez_subprocess_env()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False, timeout=30)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.warning("[Rez] health check error: %s", exc)
        return RezHealthResult(
            healthy=False,
            check_duration_ms=elapsed_ms,
            package_count=0,
            timestamp=datetime.now(UTC).isoformat(),
        )

    elapsed_ms = (time.monotonic() - start) * 1000

    if result.returncode != 0:
        logger.warning("[Rez] health check failed (exit %d)", result.returncode)
        return RezHealthResult(
            healthy=False,
            check_duration_ms=elapsed_ms,
            package_count=0,
            timestamp=datetime.now(UTC).isoformat(),
        )

    families = [line for line in result.stdout.splitlines() if line.strip()]
    logger.info("[Rez] health check OK — %d families in %.0fms", len(families), elapsed_ms)
    return RezHealthResult(
        healthy=True,
        check_duration_ms=elapsed_ms,
        package_count=len(families),
        timestamp=datetime.now(UTC).isoformat(),
    )


def get_rez_context_string() -> str:
    """Return the rez context request string from the current environment.

    When running inside a ``rez-env`` activated shell, rez sets
    ``REZ_USED_REQUEST`` to the packages that were requested (e.g.
    "griptape_FDN"). This is the short form suitable for display —
    not the full resolved tree (``REZ_USED_RESOLVE``).
    """
    return os.environ.get("REZ_USED_REQUEST", "")
