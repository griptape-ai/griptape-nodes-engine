"""Utilities for Rez environment integration."""

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.utils.file_utils import find_file_in_directory
from griptape_nodes.utils.git_utils import get_git_repository_root
from griptape_nodes.utils.rez_uv import install as rez_uv_install
from griptape_nodes.utils.rez_uv import resolve_full, rez_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables (documentation only — all values are read at runtime
# via the accessor functions below, never from these module-level constants)
# ---------------------------------------------------------------------------
#
# GTN_REZ_ROOT           — Studio root for the current platform.
#                          Presence of this variable enables rez integration.
#                          All downstream path vars are relative to this root.
#
# GTN_REZ_PATH_MAP       — Cross-platform studio root mapping.
#                          Format: linux=/mnt/pipeline;osx=/Volumes/pipeline;windows=P:
#                          Platform keys match rez's system.platform: linux, osx, windows.
#                          Optional — for GUI display and CLI tooling.
#
# GTN_REZ_BIN_PATH       — Rez binary directory, relative to GTN_REZ_ROOT.
#                          Example: rez/bin  (resolved to GTN_REZ_ROOT/rez/bin)
#                          Absolute paths are also accepted (backward compat).
#                          Unset = rely on PATH.
#
# GTN_REZ_CONFIG_FILE    — Path to rezconfig.py, relative to GTN_REZ_ROOT.
#                          Mirrors REZ_CONFIG_FILE.  Unset = rez default.
#
# GTN_REZ_LOCAL_PACKAGES_PATH — Local package install directory, relative
#                          to GTN_REZ_ROOT.  Example: rez/packages/local
#
# GTN_REZ_RELEASE_PACKAGES_PATH — Release package directory, relative
#                          to GTN_REZ_ROOT.

# ---------------------------------------------------------------------------
# Platform mapping
# ---------------------------------------------------------------------------

_PLATFORM_MAP = {"linux": "linux", "darwin": "osx", "win32": "windows"}


def current_platform_key() -> str:
    """Map ``sys.platform`` to rez's platform key (linux/osx/windows)."""
    return _PLATFORM_MAP.get(sys.platform, "linux")


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


def is_rez_enabled() -> bool:
    """Return True when rez integration is active.

    Rez is enabled when ``GTN_REZ_ROOT`` is set, indicating the engine is
    running inside a rez-managed studio environment.
    """
    return bool(os.getenv("GTN_REZ_ROOT", "").strip())


def rez_path_map() -> dict[str, str]:
    """Parse ``GTN_REZ_PATH_MAP`` into a ``{platform: root_path}`` dict.

    Format: ``linux=/mnt/pipeline;osx=/Volumes/pipeline;windows=P:``
    Returns an empty dict when the env var is unset.
    """
    raw = os.getenv("GTN_REZ_PATH_MAP", "")
    if not raw:
        return {}
    result: dict[str, str] = {}
    for raw_entry in raw.split(";"):
        stripped = raw_entry.strip()
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def rez_root() -> Path | None:
    """Return the resolved studio root from ``GTN_REZ_ROOT``, or None if unset."""
    val = os.getenv("GTN_REZ_ROOT", "").strip()
    if not val:
        return None
    return Path(_anchor_drive_letter(val))


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

    Absolute paths are returned directly — this supports local package
    stores that live outside the studio root (e.g. a user-local build
    directory). Relative paths are joined with ``GTN_REZ_ROOT`` for
    network/shared layouts. Returns None when the env var is unset.
    """
    val = os.getenv(env_var, "").strip()
    if not val:
        return None
    path = Path(val)
    if path.is_absolute():
        return path
    root = rez_root()
    if root is None:
        return path
    return root / val


def rez_bin_path() -> Path | None:
    """Return the Rez binary directory, resolved against ``GTN_REZ_ROOT`` if relative."""
    return _resolve_rez_path("GTN_REZ_BIN_PATH")


def rez_config_file() -> Path | None:
    """Return the rezconfig.py path, resolved against ``GTN_REZ_ROOT`` if relative."""
    return _resolve_rez_path("GTN_REZ_CONFIG_FILE")


def rez_local_packages_path() -> Path | None:
    """Return the local packages path, resolved against ``GTN_REZ_ROOT`` if relative."""
    return _resolve_rez_path("GTN_REZ_LOCAL_PACKAGES_PATH")


def rez_release_packages_path() -> Path | None:
    """Return the release packages path, resolved against ``GTN_REZ_ROOT`` if relative."""
    return _resolve_rez_path("GTN_REZ_RELEASE_PACKAGES_PATH")


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


def _rez_executable(command: str) -> str:
    """Return the path to a rez CLI command, respecting GTN_REZ_BIN_PATH.

    Searches ``GTN_REZ_BIN_PATH`` directly (handling Windows' ``.exe``/``.cmd``
    suffixes ourselves) before falling back to ``shutil.which`` on ``PATH``.
    """
    bin_dir = rez_bin_path()
    if bin_dir is not None:
        for candidate_name in _executable_candidate_names(command):
            candidate_path = bin_dir / candidate_name
            if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
                return str(candidate_path)
    return shutil.which(command) or command


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


def rez_search_paths() -> list[Path] | None:
    """Return the package search path rez is configured with, or None if rez cannot say.

    Read-only: runs ``rez-config --json packages_path`` in the same environment as
    every other rez command Griptape Nodes runs.
    """
    cmd = [_rez_executable("rez-config"), "--json", "packages_path"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, env=rez_subprocess_env(), check=False, timeout=30
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


def rez_unsearched_stores(stores: list[Path] | None = None) -> list[Path]:
    """Return the package stores that rez's ``packages_path`` does not include.

    Packages in such a store are found by Griptape Nodes on disk but cannot be resolved
    by ``rez env``. Defaults to the ``GTN_REZ_*`` local and release stores. Returns an
    empty list when rez cannot report its search path.
    """
    if stores is None:
        stores = _rez_store_dirs()
    if not stores:
        return []

    search_paths = rez_search_paths()
    if search_paths is None:
        return []

    searched = {canonicalize_for_identity(path) for path in search_paths}
    return [store for store in stores if canonicalize_for_identity(store) not in searched]


def _log_rez_env_context() -> None:
    """Emit debug lines describing the current Rez configuration."""
    logger.debug("[Rez] enabled=%s", is_rez_enabled())
    logger.debug("[Rez] root=%s", rez_root() or "(unset)")
    path_map = rez_path_map()
    if path_map:
        logger.debug("[Rez] path_map=%s", path_map)
    logger.debug("[Rez] bin_path=%s (raw=%s)", rez_bin_path() or "(PATH)", os.getenv("GTN_REZ_BIN_PATH") or "(unset)")
    logger.debug("[Rez] config_file=%s", rez_config_file() or "(rez default)")
    logger.debug("[Rez] local_packages_path=%s", rez_local_packages_path() or "(rez default)")
    logger.debug("[Rez] release_packages_path=%s", rez_release_packages_path() or "(rez default)")


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


def _rez_packages_root() -> Path | None:
    """Return the root of the rez package store, derived from GTN_REZ_LOCAL_PACKAGES_PATH.

    rez_uv.install() writes to ``<root>/local/<family>/<version>/``.  The
    local packages path already points at the ``local/`` subdirectory, so
    the root is its parent.
    """
    local_path = rez_local_packages_path()
    return local_path.parent if local_path is not None else None


def _rez_store_dirs() -> list[Path]:
    """Return the package stores Griptape Nodes reads, in rez's usual order: local, then release."""
    return [store for store in (rez_local_packages_path(), rez_release_packages_path()) if store is not None]


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

    Searches the local store, then the release store. When *version* is provided,
    looks for exactly that version; otherwise uses the highest version in either store.

    Args:
        rez_family: Rez package family name (no version).
        version: Optional specific version to resolve (e.g. "0.81.0").
    """
    stores = _rez_store_dirs()
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
    rez family (see ``library_file_path_to_rez_family``) in the local package store.
    """
    return get_library_rez_package_version(library_file_path) is not None


def get_library_rez_package_version(library_file_path: Path, *, packages_root: Path | None = None) -> str | None:
    """Return the latest version of a library's rez package, or None if unavailable.

    Searches the local and release stores for version directories containing a
    ``package.py`` and returns the highest version found in either.

    Args:
        library_file_path: Library JSON path. The rez family is derived from the
            library's repo or folder name (see ``library_file_path_to_rez_family``).
        packages_root: Root of a package store being built (its ``local/`` folder is
            searched instead of the ``GTN_REZ_*`` stores). Used by the build CLI.
    """
    if packages_root is None:
        stores = _rez_store_dirs()
    else:
        stores = [packages_root / "local"]
    if not stores:
        return None

    rez_family = library_file_path_to_rez_family(library_file_path)
    version_dir = _find_package_version_dir(rez_family, None, stores)
    if version_dir is None:
        return None

    version = version_dir.name
    logger.debug("[Rez] found rez package for '%s': %s-%s", library_file_path, rez_family, version)
    return version


def library_file_path_to_rez_family(library_file_path: Path) -> str:
    """Derive the rez package family name from the library's on-disk directory.

    Resolution order:
    1. Rez package store path — if the manifest is inside a rez package
       (``<store>/local/<family>/<version>/python/<manifest>``), extract
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
    # <store>/local/<family>/<version>/python/<manifest.json>
    store_family = _detect_rez_store_family(library_file_path)
    if store_family:
        logger.debug(
            "[Rez] naming: rez store path → family '%s' (json at %s)",
            store_family,
            library_file_path,
        )
        return store_family

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
                return family

            family = _library_rez_name(git_root.name)
            logger.debug(
                "[Rez] naming: git root '%s' → family '%s' (json at %s)",
                git_root.name,
                family,
                library_file_path,
            )
            return family
    except (OSError, subprocess.SubprocessError):
        logger.debug("[Rez] naming: git lookup failed for %s, falling back to parent dir", library_dir)

    family = _library_rez_name(library_dir.name)
    logger.debug("[Rez] naming: parent dir '%s' → family '%s' (no git root)", library_dir.name, family)
    return family


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

    Rez store layout: ``<store>/local/<family>/<version>/python/<manifest.json>``
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
    repo/folder-named family in the local or release store.
    """
    if _detect_rez_store_family(library_file_path) is not None:
        return library_file_path.parent.parent

    rez_family = library_file_path_to_rez_family(library_file_path)
    return _find_package_version_dir(rez_family, None, _rez_store_dirs())


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


def _derive_library_version(library_file_path: Path) -> str:
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


def _write_library_meta_package(  # noqa: PLR0913
    library_name: str,
    library_version: str,
    resolved_requires: list[str],
    packages_root: Path,
    *,
    rez_family: str,
    library_source_dir: Path,
    library_json_name: str,
    skip_installed: bool = True,
) -> None:
    """Write a rez meta-package that bundles the library source alongside its dep declarations.

    The library source is copied into ``<version_dir>/python/`` (excluding
    ``.venv``, ``.git``, ``__pycache__``, etc.) so the rez package is
    self-contained. The generated ``commands()`` block appends ``{root}/python``
    to ``PYTHONPATH``, making the library's nodes importable in any rez
    environment that resolves this package.

    Args:
        library_name: Human-readable library name (e.g. "Luma Labs Library").
        library_version: Version string for the meta-package (e.g. "1.0.0").
        resolved_requires: Pinned rez requires strings (e.g. ["torch-2.7.0", ...]).
        packages_root: Root of the rez package store (parent of ``local/``).
        rez_family: Rez family name, derived from the library's repo or folder name.
        library_source_dir: Directory containing the library's Python source
            (parent of the griptape_nodes_library.json file). Copied into the
            rez package so the package is self-contained and portable.
        library_json_name: Filename of the library manifest JSON (e.g.
            ``"griptape_nodes_library.json"``). Set as ``GTN_REZ_LIBRARY_JSON``
            in the generated ``commands()`` block.
        skip_installed: When True, skip writing if the meta-package already
            exists. When False, overwrite.
    """
    version_dir = packages_root / "local" / rez_family / library_version
    version_dir.mkdir(parents=True, exist_ok=True)

    pkg_file = version_dir / "package.py"
    if pkg_file.exists() and skip_installed:
        logger.debug("[Rez] meta-package already exists for library '%s' — skipping write", library_name)
        return

    escaped_name = library_name.replace("'", "\\'")
    req_entries = "".join(f"    '{r}',\n" for r in sorted(resolved_requires))

    python_dest = version_dir / "python"
    logger.info("[Rez] copying library source into rez package: %s → %s", library_source_dir, python_dest)
    shutil.copytree(
        library_source_dir,
        python_dest,
        ignore=_LIBRARY_COPY_EXCLUDES,
        dirs_exist_ok=True,
    )
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

requires = [
{req_entries}]

format_version = 2
{commands_block}"""
    pkg_file.write_text(content, encoding="utf-8")
    logger.info(
        "[Rez] wrote meta-package '%s-%s' with %d requires", rez_family, library_version, len(resolved_requires)
    )


def install_library_as_rez_package(  # noqa: PLR0913
    library_name: str,
    pip_dependencies: list[str],
    *,
    library_file_path: Path,
    pip_dependencies_exec: list[str] | None = None,
    extra_index_url: str | None = None,
    pip_install_flags: list[str] | None = None,
    python_version: str | None = None,
    skip_installed: bool = True,
    packages_root: Path | None = None,
) -> None:
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
        skip_installed: When True (default), skip packages whose rez package
            already exists. When False, overwrite everything.
        packages_root: Root of the rez package store (parent of ``local/``).
            Defaults to the parent of ``GTN_REZ_LOCAL_PACKAGES_PATH``.
    """
    if packages_root is None:
        packages_root = _rez_packages_root()
    if packages_root is None:
        logger.warning(
            "[Rez] GTN_REZ_LOCAL_PACKAGES_PATH is not set — cannot install library '%s' as a rez package",
            library_name,
        )
        return

    all_pip_dependencies = list(pip_dependencies)
    if pip_dependencies_exec:
        all_pip_dependencies.extend(pip_dependencies_exec)

    library_version = _derive_library_version(library_file_path)

    # A library without pip dependencies still needs its meta-package: it carries the
    # library source and manifest, which is what makes the library registrable via REZ:.
    resolved_requires: list[str] = []
    if all_pip_dependencies:
        logger.info(
            "[Rez] resolving deps for library '%s' (%d edit + %d exec specs) ...",
            library_name,
            len(pip_dependencies),
            len(pip_dependencies_exec or []),
        )

        resolved = resolve_full(
            all_pip_dependencies,
            extra_index_url=extra_index_url,
            extra_flags=pip_install_flags,
            python_version=python_version,
        )

        direct_names = {pip_spec_name(spec) for spec in all_pip_dependencies}
        resolved_requires = [
            f"{rez_name(pkg.pip_name)}-{pkg.version}" for pkg in resolved if pip_spec_name(pkg.pip_name) in direct_names
        ]

        rez_uv_install(
            all_pip_dependencies,
            packages_dir=packages_root,
            extra_index_url=extra_index_url,
            extra_flags=pip_install_flags,
            python_version=python_version,
            skip_installed=skip_installed,
        )
    else:
        logger.info("[Rez] library '%s' has no pip dependencies — writing its package only", library_name)

    # Name the package after the repo / folder it was built from (e.g.
    # griptape_nodes_library_diffusers), not the manifest's display name, so a rez
    # admin can match installed packages to their sources.
    _write_library_meta_package(
        library_name,
        library_version,
        resolved_requires,
        packages_root,
        rez_family=library_file_path_to_rez_family(library_file_path),
        library_source_dir=library_file_path.parent,
        library_json_name=library_file_path.name,
        skip_installed=skip_installed,
    )


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
