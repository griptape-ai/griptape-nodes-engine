"""Rez package installer backed by uv — a drop-in improvement over rez-pip.

Differences from rez-pip
-------------------------
- Uses ``uv pip compile`` for full transitive dependency resolution (co-resolves
  all requested packages in one call so the solver produces one consistent lock).
- Parses uv's annotated output to build an exact direct-dependency graph; that
  graph becomes each package's ``requires`` list rather than the incomplete
  wheel METADATA declaration.
- Uses ``uv pip install --no-deps --target`` to download each package's files in
  isolation (no bundled transitive deps).
- Generates ``package.py`` from scratch with correct, complete ``requires``.
- Normalises all family names to lowercase+underscores, eliminating the
  ruamel.yaml vs ruamel_yaml naming mismatch that rez-pip produces.

Logging verbosity is inherited from the root logger configuration.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path

from packaging.markers import InvalidMarker, Marker

from griptape_nodes.utils.uv_utils import find_uv_bin

logger = logging.getLogger(__name__)

# Wheel tag has exactly three dash-separated components: py-abi-platform.
_WHEEL_TAG_PARTS = 3


class RezInstallError(RuntimeError):
    """One or more resolved packages could not be installed as rez packages.

    Raised after every other package has been installed, so callers can stop before
    writing a package whose ``requires`` names packages that are missing.
    """

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__(f"{len(failures)} package(s) could not be installed: " + "; ".join(failures))


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


def rez_name(pip_name: str) -> str:
    """Normalise a pip package name to a rez family name.

    Convention: lowercase, hyphens / dots / underscores all → underscore.
    Consistent normalisation means the package family directory name and the
    requires entries always agree — eliminates the ruamel.yaml vs ruamel_yaml
    gap that rez-pip produces.
    """
    return re.sub(r"[-._]+", "_", pip_name).lower()


def pip_normalize(name: str) -> str:
    """PEP 503 canonical name: lowercase, all separators → hyphen."""
    return re.sub(r"[-._]+", "-", name).lower()


# ---------------------------------------------------------------------------
# Resolution — full dep tree + direct-dep graph
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPackage:
    pip_name: str  # name as uv resolved it
    version: str  # clean version (no +local suffix)
    direct_deps: set[str] = field(default_factory=set)  # pip-normalised dep names
    local_version: str | None = None  # the +local suffix uv resolved, e.g. "cu128" for torch 2.7.0+cu128

    @property
    def torch_backend(self) -> str | None:
        """The torch build this wheel is for (``cu128``, ``cpu``, ...), from its local version."""
        return torch_backend_from_local_version(self.local_version)


@dataclass
class ResolvedEntry:
    """One ``name==version`` line of uv's compile output."""

    pip_name: str
    version: str
    local_version: str | None


def _parse_annotation_graph(stdout: str) -> tuple[list[ResolvedEntry], dict[str, set[str]]]:
    """Parse uv's annotated ``pip compile`` output.

    Returns ``(pkg_list, requirers_of)`` where:

    - ``pkg_list`` lists the resolved packages in resolution order, with the local
      version suffix (``+cu128``) split off the version.
    - ``requirers_of`` maps a dep's pip-normalised name to the set of
      pip-normalised names of packages that directly require it.
    """
    pkg_list: list[ResolvedEntry] = []
    requirers_of: dict[str, set[str]] = defaultdict(set)
    current_norm: str | None = None
    in_via = False

    for line in stdout.splitlines():
        stripped = line.strip()

        if not line.startswith(" ") and "==" in stripped:
            name, ver = stripped.split("==", 1)
            clean_ver, _, local = ver.partition("+")
            pkg_list.append(ResolvedEntry(pip_name=name, version=clean_ver, local_version=local or None))
            current_norm = pip_normalize(name)
            in_via = False

        elif current_norm and stripped.startswith("# via"):
            tail = stripped[len("# via") :].strip()
            in_via = True
            if tail and not tail.startswith("-r"):
                requirers_of[current_norm].add(pip_normalize(tail))
                in_via = False

        elif current_norm and in_via and stripped.startswith("#"):
            r = stripped.lstrip("#").strip()
            if r and not r.startswith("-r") and r != "via":
                requirers_of[current_norm].add(pip_normalize(r))

        else:
            in_via = False

    return pkg_list, requirers_of


def resolve_full(
    packages: str | list[str],
    *,
    extra_index_url: str | None = None,
    extra_flags: list[str] | None = None,
    python_version: str | None = None,
    uv_cmd: str | None = None,
) -> list[ResolvedPackage]:
    """Resolve *packages* and return the complete transitive dependency set.

    All packages are co-resolved in a single uv invocation so the solver
    produces one consistent lock set.  uv's annotated output is parsed to
    build the direct-dep graph -- this becomes each package's ``requires``
    list in its rez ``package.py``.
    """
    uv = uv_cmd or find_uv_bin()
    if isinstance(packages, str):
        packages = [packages]
    if python_version is None:
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    label = ", ".join(packages)
    logger.info("[Rez][uv] resolving [%s] (python %s) ...", label, python_version)
    logger.debug("[Rez][uv] uv binary: %s", uv)

    cmd = [uv, "pip", "compile", "--no-header", "-", "--python-version", python_version]
    if extra_index_url:
        cmd += ["--extra-index-url", extra_index_url]
        logger.debug("[Rez][uv] extra index: %s", extra_index_url)
    if extra_flags:
        cmd += extra_flags
        logger.debug("[Rez][uv] extra flags: %s", extra_flags)

    result = subprocess.run(  # noqa: S603
        cmd,
        input="\n".join(packages) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )

    logger.debug("[Rez][uv] uv pip compile stdout (%d chars)", len(result.stdout))

    pkg_list, requirers_of = _parse_annotation_graph(result.stdout)

    # Invert requirers_of → {pkg: set_of_direct_deps}
    direct_deps_of: dict[str, set[str]] = defaultdict(set)
    for dep_norm, requirers in requirers_of.items():
        for req in requirers:
            direct_deps_of[req].add(dep_norm)

    resolved = [
        ResolvedPackage(
            pip_name=entry.pip_name,
            version=entry.version,
            direct_deps=direct_deps_of.get(pip_normalize(entry.pip_name), set()),
            local_version=entry.local_version,
        )
        for entry in pkg_list
    ]

    logger.info("[Rez][uv] resolved %d packages", len(resolved))
    if logger.isEnabledFor(logging.DEBUG):
        for pkg in resolved:
            dep_str = ", ".join(sorted(pkg.direct_deps)) or "—"
            logger.debug("[Rez][uv]   %s==%s  deps=[%s]", pkg.pip_name, pkg.version, dep_str)

    return resolved


# ---------------------------------------------------------------------------
# Wheel metadata
# ---------------------------------------------------------------------------


@dataclass
class WheelInfo:
    pip_name: str  # original PyPI name from METADATA (may be mixed-case)
    version: str
    description: str
    authors: list[str]
    requires_python: str | None
    console_scripts: list[str]  # tool names from entry_points
    is_pure_python: bool
    platform_tag: str  # e.g. "macosx_11_0_arm64" or "any"
    requires_dist: list[str]  # raw Requires-Dist strings from METADATA


def _parse_entry_points(text: str) -> list[str]:
    tools: list[str] = []
    in_console = False
    for line in text.splitlines():
        s = line.strip()
        if s == "[console_scripts]":
            in_console = True
        elif s.startswith("["):
            in_console = False
        elif in_console and "=" in s:
            tools.append(s.split("=")[0].strip())
    return tools


def _detect_wheel_tag(dist_info: Path) -> tuple[bool, str]:
    """Return ``(is_pure_python, platform_tag)`` from the WHEEL file."""
    wheel_file = dist_info / "WHEEL"
    if not wheel_file.exists():
        return True, "any"

    for line in wheel_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("tag:"):
            tag = line.split(":", 1)[1].strip()
            parts = tag.split("-")
            if len(parts) == _WHEEL_TAG_PARTS:
                platform_tag = parts[2]
                return platform_tag == "any", platform_tag

    return True, "any"


def read_wheel_info(install_dir: Path, pip_name: str, version: str) -> WheelInfo:
    """Read metadata from a directory produced by ``uv pip install --target``."""
    dist_info: Path | None = None
    for entry in install_dir.iterdir():
        if entry.is_dir() and entry.name.endswith(".dist-info"):
            dist_info = entry
            break
    if dist_info is None:
        msg = f"No .dist-info found in {install_dir} for {pip_name}=={version}"
        raise RuntimeError(msg)

    logger.debug("[Rez][uv]   reading metadata from %s", dist_info.name)

    meta_text = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
    msg = HeaderParser().parsestr(meta_text)

    raw_name = msg.get("Name", pip_name).strip()
    summary = (msg.get("Summary") or "").strip()

    authors: list[str] = []
    for field_name in ("Author-email", "Author"):
        val = msg.get(field_name)
        if val:
            authors.append(val.strip())

    requires_python: str | None = msg.get("Requires-Python")
    if requires_python:
        requires_python = requires_python.strip()

    ep_file = dist_info / "entry_points.txt"
    console_scripts = (
        _parse_entry_points(ep_file.read_text(encoding="utf-8", errors="replace")) if ep_file.exists() else []
    )

    is_pure, platform_tag = _detect_wheel_tag(dist_info)
    requires_dist = msg.get_all("Requires-Dist") or []

    logger.debug(
        "[Rez][uv]   %s==%s  pure=%s  platform=%s  tools=%s",
        raw_name,
        version,
        is_pure,
        platform_tag,
        console_scripts or "none",
    )

    return WheelInfo(
        pip_name=raw_name,
        version=version,
        description=summary,
        authors=authors[:2],
        requires_python=requires_python,
        console_scripts=console_scripts,
        is_pure_python=is_pure,
        platform_tag=platform_tag,
        requires_dist=requires_dist,
    )


# ---------------------------------------------------------------------------
# Rez platform helpers
# ---------------------------------------------------------------------------


def _current_rez_platform() -> tuple[str, str]:
    """Return ``(rez_platform, rez_arch)`` for the running machine."""
    machine = os.uname().machine if hasattr(os, "uname") else "x86_64"
    if sys.platform == "darwin":
        return ("osx", "arm64" if machine == "arm64" else "x86_64")
    if sys.platform.startswith("linux"):
        return ("linux", "x86_64" if machine in ("x86_64", "amd64") else machine)
    if sys.platform == "win32":
        return ("windows", os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"))
    return ("linux", "x86_64")


def current_platform_key() -> str:
    """This machine as a ``<platform>-<arch>`` key, the way rez names it (``osx-arm64``)."""
    rez_platform, rez_arch = _current_rez_platform()
    return f"{rez_platform}-{rez_arch}"


# The platforms studios run, as PEP 508 environment markers see them. A package's
# platform-conditional dependencies are worked out for each of these at build time, so a
# package built on one platform lists the right dependencies for the others too.
PLATFORM_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "osx-arm64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "os_name": "posix",
        "platform_machine": "arm64",
    },
    "osx-x86_64": {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "os_name": "posix",
        "platform_machine": "x86_64",
    },
    "linux-x86_64": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
    },
    "linux-aarch64": {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "aarch64",
    },
    "windows-AMD64": {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "platform_machine": "AMD64",
    },
}

# Key in ``platform_requires`` used on a platform that is not listed.
FALLBACK_PLATFORM_KEY = "*"

_PLATFORM_MARKER = re.compile(r"\b(sys_platform|platform_system|os_name|platform_machine)\b")


# ---------------------------------------------------------------------------
# torch_backend tiers
# ---------------------------------------------------------------------------

# Rez ephemeral naming the torch build a variant holds: ``.torch_backend-cu128``.
TORCH_BACKEND_EPHEMERAL = "torch_backend"

# The torch builds uv knows, oldest first; ``--torch-backend all`` tries each.
TORCH_BACKENDS: tuple[str, ...] = ("cpu", "cu118", "cu121", "cu124", "cu126", "cu128", "cu129", "cu130")

PYTORCH_INDEX_URL = "https://download.pytorch.org/whl/{backend}"

_TORCH_BACKEND_PATTERN = re.compile(r"^(cpu|cu\d+|rocm\d+(\.\d+)*|xpu)$")
_PYTORCH_INDEX = re.compile(r"download\.pytorch\.org/whl/")


def torch_backend_from_local_version(local_version: str | None) -> str | None:
    """Return the torch build named by a wheel's local version (``cu128`` in ``2.7.0+cu128``), or None."""
    if local_version is None or _TORCH_BACKEND_PATTERN.match(local_version) is None:
        return None
    return local_version


def torch_backend_request(backend: str) -> str:
    """The rez request for a torch build: ``.torch_backend-cu128``."""
    return f".{TORCH_BACKEND_EPHEMERAL}-{backend}"


def flags_for_torch_backend(flags: list[str] | None, backend: str) -> list[str]:
    """Return install flags that take torch from the PyTorch index for *backend*.

    Any torch source already in *flags* is replaced: ``--torch-backend``, and
    ``--extra-index-url`` / ``--index-url`` pointing at ``download.pytorch.org/whl/``
    (a library may pin one, e.g. ``cu128``). Other flags are kept. Every other package
    still comes from PyPI, so ``--index-strategy unsafe-best-match`` is added when absent.
    """
    items = list(flags or [])
    result: list[str] = []
    index = 0
    while index < len(items):
        flag = items[index]
        name, separator, value = flag.partition("=")
        step = 1 if separator else 2
        if name == "--torch-backend":
            index += step
            continue
        if name in ("--extra-index-url", "--index-url"):
            url = value if separator else (items[index + 1] if index + 1 < len(items) else "")
            if _PYTORCH_INDEX.search(url):
                index += step
                continue
        result.append(flag)
        index += 1

    result += ["--extra-index-url", PYTORCH_INDEX_URL.format(backend=backend)]
    if not any(flag.partition("=")[0] == "--index-strategy" for flag in result):
        result += ["--index-strategy", "unsafe-best-match"]
    return result


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


@dataclass
class ParsedRequirement:
    """A PEP 508 requirement split into name, version specifier, and environment marker."""

    pip_name: str
    specifier: str
    marker: str | None

    @property
    def depends_on_platform(self) -> bool:
        return self.marker is not None and _PLATFORM_MARKER.search(self.marker) is not None


def parse_requirement(req_str: str) -> ParsedRequirement | None:
    """Parse a requirement string without evaluating its marker. Extras are stripped."""
    req = req_str.strip()
    marker: str | None = None
    if ";" in req:
        spec_part, _, marker_str = req.partition(";")
        marker = marker_str.strip() or None
        req = spec_part.strip()
    req = re.sub(r"\[.*?\]", "", req)  # strip extras
    # "name (>=1.0,<2)" form
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*\(([^)]*)\)\s*$", req)
    if m:
        return ParsedRequirement(pip_name=m.group(1).strip(), specifier=m.group(2).strip(), marker=marker)
    # "name>=1.0,<2" or bare "name" form
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)\s*$", req)
    if m:
        return ParsedRequirement(pip_name=m.group(1).strip(), specifier=m.group(2).strip(), marker=marker)
    return None


def marker_applies(marker_str: str | None, environment: dict[str, str] | None = None) -> bool:
    """Evaluate a PEP 508 marker for this machine, or for *environment* (values override this machine's)."""
    if marker_str is None:
        return True
    try:
        return Marker(marker_str).evaluate(environment)
    except (InvalidMarker, ValueError):
        return False


def platform_environments() -> dict[str, dict[str, str] | None]:
    """Every known platform's marker environment, plus this machine's real one (None) when it is not listed."""
    environments: dict[str, dict[str, str] | None] = dict(PLATFORM_ENVIRONMENTS)
    environments.setdefault(current_platform_key(), None)
    return environments


def _parse_requires_dist(req_str: str) -> tuple[str, str] | None:
    """Parse a Requires-Dist string into (pip_name, specifier_str) for this machine.

    Conditional deps (those with environment markers) are evaluated against
    the current platform — included when the marker matches, dropped otherwise.
    Extras in the package name (e.g. ``requests[security]``) are stripped.
    """
    parsed = parse_requirement(req_str)
    if parsed is None or not marker_applies(parsed.marker):
        return None
    return parsed.pip_name, parsed.specifier


# Minimum number of version components for a ~= compatible-release with a patch bound
_COMPATIBLE_RELEASE_PATCH_PARTS = 3


def _pep440_spec_to_rez(pip_dep_name: str, specifier_str: str) -> str:  # noqa: C901, PLR0911, PLR0912
    """Convert a pip package name + PEP 440 specifier string to a rez range token.

    Mapping rules:
      (no spec)       → pkg
      >=A,<B          → pkg-A+<B
      >=A             → pkg-A+
      >A              → pkg-A+   (rez has no strict-lower; treat same as >=)
      <B              → pkg<B
      <=B             → pkg<B    (rez has no <=; close enough for a range)
      ==X.Y.Z         → pkg==X.Y.Z
      ==X.Y.*         → pkg-X.Y
      ~=X.Y           → pkg-X.Y+<X+1
      ~=X.Y.Z         → pkg-X.Y.Z+<X.Y+1
      !=X             → pkg       (rez cannot express != cleanly; drop it)
    """
    rez_pkg = rez_name(pip_dep_name)
    specifier_str = specifier_str.strip()
    if not specifier_str:
        return rez_pkg

    specs = [s.strip() for s in specifier_str.split(",") if s.strip()]
    lower: str | None = None
    upper: str | None = None
    exact: str | None = None
    series: str | None = None

    for spec in specs:
        m = re.match(r"^(~=|==|!=|>=|<=|>|<)\s*(.+)$", spec)
        if not m:
            continue
        op, ver = m.group(1), re.sub(r"\+.*$", "", m.group(2).strip())

        if op == "==":
            if ver.endswith(".*"):
                series = ver[:-2]  # "1.2.*" → the 1.2 series; rez "pkg-1.2" matches 1.2.x only
            else:
                exact = ver
        elif op == "~=":
            parts = ver.split(".")
            if len(parts) >= _COMPATIBLE_RELEASE_PATCH_PARTS:
                # ~=X.Y.Z → >=X.Y.Z, <X.Y+1
                lower = ver
                upper_parts = parts[:-1]
                num_match = re.match(r"^(\d+)", upper_parts[-1])
                upper_parts[-1] = str(int(num_match.group(1)) + 1) if num_match else upper_parts[-1]
                upper = ".".join(upper_parts)
            else:
                # ~=X.Y → >=X.Y, <X+1
                lower = ver
                num_match = re.match(r"^(\d+)", parts[0])
                upper = str(int(num_match.group(1)) + 1) if num_match else parts[0]
        elif op in (">=", ">"):
            lower = ver
        elif op in ("<=", "<"):
            upper = ver
        # != is skipped — rez has no exclusion syntax

    if exact is not None:
        return f"{rez_pkg}=={exact}"
    if series is not None:
        return f"{rez_pkg}-{series}"
    if lower is not None and upper is not None:
        return f"{rez_pkg}-{lower}+<{upper}"
    if lower is not None:
        return f"{rez_pkg}-{lower}+"
    if upper is not None:
        return f"{rez_pkg}<{upper}"
    return rez_pkg


def rez_range(requirement: ParsedRequirement) -> str:
    """A requirement as a rez request, keeping its version range."""
    return _pep440_spec_to_rez(requirement.pip_name, requirement.specifier)


@dataclass
class WheelRequires:
    """A wheel's dependencies as rez requests.

    ``common`` applies on every platform. ``platform`` lists, per platform key, the
    dependencies whose markers depend on the platform; it is empty when none do.
    """

    common: list[str]
    platform: dict[str, list[ParsedRequirement]]


def wheel_requires(requires_dist: list[str]) -> WheelRequires:
    """Split a wheel's Requires-Dist into dependencies for every platform and per-platform ones.

    Markers on anything but the platform (Python version, extras) are evaluated for this
    machine. Platform markers are evaluated for every platform in ``PLATFORM_ENVIRONMENTS``.
    """
    common: list[str] = []
    platform_specific: list[ParsedRequirement] = []
    for req_str in requires_dist:
        parsed = parse_requirement(req_str)
        if parsed is None:
            continue
        if parsed.depends_on_platform:
            platform_specific.append(parsed)
        elif marker_applies(parsed.marker):
            common.append(rez_range(parsed))

    per_platform: dict[str, list[ParsedRequirement]] = {}
    if platform_specific:
        for key, environment in platform_environments().items():
            per_platform[key] = [r for r in platform_specific if marker_applies(r.marker, environment)]
    return WheelRequires(common=sorted(common), platform=per_platform)


def merge_platform_requires(
    existing: dict[str, list[str]] | None,
    computed: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merge a build's per-platform requires into a package's existing ones.

    This machine's build is authoritative for this machine's key. For every other key an
    existing entry is kept (that platform's own build wrote it); missing keys are filled in.
    """
    this_key = current_platform_key()
    merged = dict(existing or {})
    for key, value in computed.items():
        if key == this_key or key not in merged:
            merged[key] = value
    return merged


def _pinned_request(requirement: ParsedRequirement, resolved_versions: dict[str, str]) -> str:
    """Pin *requirement* to the version this build resolved, as a folder-safe rez request.

    Used inside variants, whose entries become folder names: a range such as ``pkg-1+<2``
    contains ``<``, which Windows does not allow in a folder name.
    """
    version = resolved_versions.get(pip_normalize(requirement.pip_name))
    if version is not None:
        return f"{rez_name(requirement.pip_name)}-{version}"
    return re.sub(r"<.*$", "", rez_range(requirement))


# ---------------------------------------------------------------------------
# Variants and package.py
# ---------------------------------------------------------------------------


@dataclass
class PackageFile:
    """The parts of an existing package.py that a new build merges with."""

    variants: list[list[str]]
    platform_requires: dict[str, list[str]] | None


def read_package_file(pkg_file: Path) -> PackageFile:
    """Read ``variants`` and ``platform_requires`` from a package.py without executing it."""
    if not pkg_file.exists():
        return PackageFile(variants=[], platform_requires=None)
    try:
        tree = ast.parse(pkg_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        logger.debug("[Rez][uv] could not parse %s", pkg_file)
        return PackageFile(variants=[], platform_requires=None)

    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("variants", "platform_requires"):
                try:
                    values[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    logger.debug("[Rez][uv] %s in %s is not a literal", target.id, pkg_file)

    variants = values.get("variants")
    platform_requires = values.get("platform_requires")
    return PackageFile(
        variants=[list(variant) for variant in variants] if isinstance(variants, list) else [],
        platform_requires=platform_requires if isinstance(platform_requires, dict) else None,
    )


def _read_existing_variants(pkg_file: Path) -> list[list[str]]:
    """Read the variants list from an existing package.py, if present."""
    return read_package_file(pkg_file).variants


def build_variant(*, is_pure: bool, python_version: str, backend: str | None, extras: list[str]) -> list[str]:
    """The variant this machine's build of a package installs into.

    Pure-Python packages share one variant across platforms. Compiled packages get this
    platform and arch, the torch build when the wheel is for one, and their
    platform-specific dependencies pinned.
    """
    if is_pure:
        return [f"python-{python_version}"]
    rez_platform, rez_arch = _current_rez_platform()
    variant = [f"platform-{rez_platform}", f"arch-{rez_arch}", f"python-{python_version}"]
    if backend is not None:
        variant.append(torch_backend_request(backend))
    return variant + extras


def _variant_identity(variant: list[str]) -> tuple[str, ...]:
    """What makes two variants the same slot: platform, arch, python, and torch build."""
    prefixes = ("platform-", "arch-", "python-", f".{TORCH_BACKEND_EPHEMERAL}-")
    return tuple(entry for entry in variant if entry.startswith(prefixes))


def _variant_subpath(variant: list[str]) -> Path:
    """The folder rez uses for a variant: one folder per variant entry."""
    return Path(*variant)


def _merge_variant(existing: list[list[str]], new_variant: list[str]) -> list[list[str]]:
    """Add a variant, replacing one for the same platform, arch, python, and torch build."""
    identity = _variant_identity(new_variant)
    merged: list[list[str]] = []
    replaced = False
    for variant in existing:
        if _variant_identity(variant) == identity:
            merged.append(new_variant)
            replaced = True
        else:
            merged.append(variant)
    if not replaced:
        merged.append(new_variant)
    return merged


def _variant_usable_here(variant: list[str], *, python_version: str, backend: str | None) -> bool:
    """Whether this machine would resolve *variant* for a package built for *backend*."""
    rez_platform, rez_arch = _current_rez_platform()
    for entry in _variant_identity(variant):
        name, _, value = entry.lstrip(".").partition("-")
        expected = {
            "platform": rez_platform,
            "arch": rez_arch,
            "python": python_version,
            TORCH_BACKEND_EPHEMERAL: backend,
        }[name]
        if value != expected:
            return False
    has_backend = any(entry.startswith(f".{TORCH_BACKEND_EPHEMERAL}-") for entry in variant)
    is_compiled = any(entry.startswith("platform-") for entry in variant)
    # A build for a torch backend needs that backend's variant, not an untiered compiled one.
    return not (backend is not None and is_compiled and not has_backend)


def _write_package_py(  # noqa: PLR0913
    version_dir: Path,
    family_name: str,
    info: WheelInfo,
    requires: list[str],
    *,
    variant: list[str],
    platform_requires: dict[str, list[str]] | None = None,
) -> None:
    pkg_file = version_dir / "package.py"
    existing = read_package_file(pkg_file)
    merged = _merge_variant(existing.variants, variant)
    variants_str = repr(merged)

    if platform_requires:
        entries = "".join(f"    {key!r}: {sorted(value)!r},\n" for key, value in sorted(platform_requires.items()))
        requires_block = f"""
# Dependencies differ by platform; rez picks this machine's list when it resolves.
platform_requires = {{
{entries}}}


@late()
def requires():
    key = system.platform + '-' + system.arch
    return this.platform_requires.get(key, this.platform_requires.get('{FALLBACK_PLATFORM_KEY}', []))
"""
    elif requires:
        entries = "".join(f"    '{r}',\n" for r in requires)
        requires_block = f"\nrequires = [\n{entries}]\n"
    else:
        requires_block = ""

    tools_block = ""
    if info.console_scripts:
        tools_str = ", ".join(f"'{t}'" for t in info.console_scripts)
        tools_block = f"\ntools = [{tools_str}]\n"

    commands_lines = ["    env.PYTHONPATH.append('{root}/python')"]
    if info.console_scripts:
        commands_lines.append("    env.PATH.append('{root}/bin')")
        commands_lines.append("    env.PATH.append('{root}/Scripts')")
    commands_body = "\n".join(commands_lines)

    desc = info.description.replace("'", "\\'")[:200] if info.description else ""
    authors_str = ", ".join("'{}'".format(a.replace("'", "\\'")) for a in info.authors)

    content = f"""\
# -*- coding: utf-8 -*-
# Generated by griptape_nodes.utils.rez_uv — do not edit by hand.

name = '{family_name}'

version = '{info.version}'
"""
    if desc:
        content += f"\ndescription = '{desc}'\n"
    if info.authors:
        content += f"\nauthors = [{authors_str}]\n"
    content += requires_block
    content += f"\nvariants = {variants_str}\n"
    content += tools_block
    content += f"""
def commands():
{commands_body}

from_pip = True

pip_name = '{info.pip_name} ({info.version})'

is_pure_python = {info.is_pure_python}

format_version = 2
"""

    pkg_file.parent.mkdir(parents=True, exist_ok=True)
    pkg_file.write_text(content, encoding="utf-8")
    if len(merged) > 1:
        logger.info("[Rez][uv]   wrote %s (%d variants)", pkg_file, len(merged))
    else:
        logger.debug("[Rez][uv]   wrote %s", pkg_file)


# ---------------------------------------------------------------------------
# Package store helpers
# ---------------------------------------------------------------------------

# How a resolved package relates to what the store already holds.
STATE_NEW = "new"  # no package of this version yet
STATE_ADD_VARIANT = "add_variant"  # the version exists, but not for this machine
STATE_INSTALLED = "installed"  # the version exists with a variant this machine uses


def install_state(
    pip_name: str,
    version: str,
    packages_dir: Path,
    *,
    python_version: str | None = None,
    backend: str | None = None,
) -> str:
    """Return ``STATE_NEW``, ``STATE_ADD_VARIANT``, or ``STATE_INSTALLED`` for a package version.

    Installed means a variant this machine resolves (same platform, arch, python, and torch
    build, or the shared pure-Python variant) is listed in ``package.py`` and its folder exists.
    """
    if python_version is None:
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    version_dir = packages_dir / rez_name(pip_name) / version
    pkg_file = version_dir / "package.py"
    if not pkg_file.exists():
        return STATE_NEW
    for variant in read_package_file(pkg_file).variants:
        usable = _variant_usable_here(variant, python_version=python_version, backend=backend)
        if usable and (version_dir / _variant_subpath(variant)).is_dir():
            return STATE_INSTALLED
    return STATE_ADD_VARIANT


def is_installed(
    pip_name: str,
    version: str,
    packages_dir: Path,
    *,
    python_version: str | None = None,
    backend: str | None = None,
) -> bool:
    """Return True if this package version already has a variant this machine uses."""
    state = install_state(pip_name, version, packages_dir, python_version=python_version, backend=backend)
    return state == STATE_INSTALLED


def _other_platforms(pip_name: str, version: str, packages_dir: Path) -> set[str]:
    """Platforms other than this one that an existing package version was built for (``osx/arm64``)."""
    this_platform = current_platform_key().replace("-", "/", 1)
    found: set[str] = set()
    pkg_file = packages_dir / rez_name(pip_name) / version / "package.py"
    for variant in read_package_file(pkg_file).variants:
        platform = next((entry.removeprefix("platform-") for entry in variant if entry.startswith("platform-")), None)
        arch = next((entry.removeprefix("arch-") for entry in variant if entry.startswith("arch-")), None)
        if platform is not None and arch is not None and f"{platform}/{arch}" != this_platform:
            found.add(f"{platform}/{arch}")
    return found


def _copy_payload(install_dir: Path, payload_root: Path, console_scripts: list[str]) -> None:
    """Copy wheel files from *install_dir* into the rez payload layout.

    Python modules → ``payload_root/python/``
    Console scripts → ``payload_root/bin/``
    """
    python_dir = payload_root / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    for src in install_dir.iterdir():
        name = src.name
        if name.endswith(".data") or name in ("__pycache__", "bin"):
            continue
        dst = python_dir / name
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)

    bin_candidates = [install_dir / "bin", install_dir / "Scripts"]
    for bin_src in bin_candidates:
        if bin_src.exists() and console_scripts:
            bin_dst = payload_root / "bin"
            bin_dst.mkdir(exist_ok=True)
            for script_name in console_scripts:
                for candidate in bin_src.glob(f"{script_name}*"):
                    shutil.copy2(candidate, bin_dst / candidate.name)


def write_rez_package(  # noqa: PLR0913
    install_dir: Path,
    info: WheelInfo,
    *,
    packages_dir: Path,
    python_version: str,
    backend: str | None = None,
    resolved_versions: dict[str, str] | None = None,
) -> Path:
    """Turn one installed wheel (``uv pip install --target``) into this machine's variant of a rez package.

    The package gets this machine's variant; variants other machines added are kept.
    Returns the package's version folder.
    """
    family_name = rez_name(info.pip_name)
    clean_version = re.sub(r"\+.*$", "", info.version)
    info.version = clean_version
    version_dir = packages_dir / family_name / clean_version

    requires_info = wheel_requires(info.requires_dist)
    this_key = current_platform_key()
    existing = read_package_file(version_dir / "package.py")
    if info.is_pure_python:
        # One variant serves every platform, so platform-specific dependencies are
        # chosen when the package resolves.
        extras: list[str] = []
        platform_requires = _pure_platform_requires(requires_info, existing.platform_requires)
        backend = None
    else:
        # This platform's own variant carries its platform-specific dependencies.
        extras = [
            _pinned_request(requirement, resolved_versions or {})
            for requirement in requires_info.platform.get(this_key, [])
        ]
        platform_requires = None
        if existing.platform_requires is not None:
            platform_requires = merge_platform_requires(existing.platform_requires, {this_key: requires_info.common})

    variant = build_variant(is_pure=info.is_pure_python, python_version=python_version, backend=backend, extras=extras)
    logger.debug("[Rez][uv]   family=%s  variant=%s", family_name, variant)

    _write_package_py(
        version_dir,
        family_name,
        info,
        requires_info.common,
        variant=variant,
        platform_requires=platform_requires,
    )
    _copy_payload(install_dir, version_dir / _variant_subpath(variant), info.console_scripts)
    return version_dir


def _install_one(  # noqa: PLR0913
    pkg: ResolvedPackage,
    *,
    uv: str,
    packages_dir: Path,
    extra_index_url: str | None,
    extra_flags: list[str] | None,
    python_version: str,
    resolved_versions: dict[str, str] | None = None,
) -> str | None:
    """Download and install a single resolved package as a rez package.

    The package gets this machine's variant; variants other machines added are kept.
    Returns None on success, otherwise a short reason the package could not be installed.
    """
    logger.info("[Rez][uv] installing %s==%s ...", pkg.pip_name, pkg.version)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # A local version pins the exact build uv resolved (torch 2.7.0+cu128, not +cu118).
        requested = f"{pkg.version}+{pkg.local_version}" if pkg.local_version else pkg.version
        dl_cmd = [
            uv,
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(tmp),
            f"{pkg.pip_name}=={requested}",
        ]
        if extra_index_url:
            dl_cmd += ["--extra-index-url", extra_index_url]
        if extra_flags:
            dl_cmd += extra_flags

        logger.debug("[Rez][uv]   download cmd: %s", " ".join(dl_cmd))
        dl = subprocess.run(dl_cmd, capture_output=True, text=True, check=False)  # noqa: S603

        if dl.returncode != 0:
            logger.warning(
                "[Rez][uv] download failed for %s==%s — skipping",
                pkg.pip_name,
                pkg.version,
            )
            stderr_lines = [line for line in (dl.stderr or "").strip().splitlines() if line.strip()]
            for line in stderr_lines[-5:]:
                logger.debug("[Rez][uv]   stderr: %s", line)
            if stderr_lines:
                return f"download failed: {stderr_lines[-1].strip()}"
            return f"download failed (uv exited with {dl.returncode})"

        try:
            info = read_wheel_info(tmp, pkg.pip_name, pkg.version)
        except (RuntimeError, OSError) as exc:
            logger.warning(
                "[Rez][uv] could not read wheel metadata for %s==%s — skipping",
                pkg.pip_name,
                pkg.version,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return f"could not read its wheel metadata: {exc}"

        try:
            version_dir = write_rez_package(
                tmp,
                info,
                packages_dir=packages_dir,
                python_version=python_version,
                backend=pkg.torch_backend,
                resolved_versions=resolved_versions,
            )
            logger.info("[Rez][uv] installed %s==%s → %s", pkg.pip_name, pkg.version, version_dir)
        except OSError as exc:
            # Package-store writes fail on permissions, full disks, and network filesystems
            # that have not yet made a freshly created parent directory visible. Name the path
            # and the OS reason so the failure is actionable without re-running at DEBUG.
            target = packages_dir / rez_name(info.pip_name) / re.sub(r"\+.*$", "", info.version)
            logger.error(
                "[Rez][uv] failed to write rez package for %s==%s at %s: %s",
                pkg.pip_name,
                pkg.version,
                exc.filename or target,
                exc.strerror or exc,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return f"could not write {exc.filename or target}: {exc.strerror or exc}"

    return None


def _pure_platform_requires(
    requires_info: WheelRequires, existing: dict[str, list[str]] | None
) -> dict[str, list[str]] | None:
    """Per-platform requires for a pure-Python package, or None when they are the same everywhere."""
    if not requires_info.platform and existing is None:
        return None
    computed = {
        key: sorted(requires_info.common + [rez_range(r) for r in requirements])
        for key, requirements in requires_info.platform.items()
    }
    computed.setdefault(current_platform_key(), list(requires_info.common))
    computed.setdefault(FALLBACK_PLATFORM_KEY, list(requires_info.common))
    return merge_platform_requires(existing, computed)


# ---------------------------------------------------------------------------
# Main install orchestration
# ---------------------------------------------------------------------------


@dataclass
class InstallReport:
    """What an install added to the package store."""

    new: list[str] = field(default_factory=list)
    added_variant: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    already_installed: list[str] = field(default_factory=list)
    other_platforms: set[str] = field(default_factory=set)

    def merge(self, other: InstallReport) -> None:
        self.new += other.new
        self.added_variant += other.added_variant
        self.rebuilt += other.rebuilt
        self.already_installed += other.already_installed
        self.other_platforms |= other.other_platforms


def install(  # noqa: PLR0913
    packages: str | list[str],
    *,
    packages_dir: Path,
    extra_index_url: str | None = None,
    extra_flags: list[str] | None = None,
    python_version: str | None = None,
    skip_installed: bool = True,
    uv_cmd: str | None = None,
) -> InstallReport:
    """Resolve and install *packages* and all transitive deps as rez packages.

    All packages are co-resolved in a single uv invocation so the solver
    produces one consistent lock set.  Each package's files are downloaded in
    isolation (``--no-deps``) so transitive dep boundaries are preserved and
    visible to the rez resolver.

    A store another platform (or torch build) already built into is added to: a
    package version that lacks this machine's variant gets it, and nothing another
    machine installed is removed.

    Parameters
    ----------
    packages:
        One or more pip package specs (e.g. ``"torch==2.7.0"`` or
        ``["torch==2.7.0", "torchvision==0.22.0"]``).  Multiple packages are
        co-resolved, which is required when they must agree on shared deps.
    packages_dir:
        The rez package store.  Packages are written to
        ``packages_dir/<family>/<version>/``.
    extra_index_url:
        Additional pip index URL (e.g. a PyTorch CUDA wheel mirror).
    python_version:
        Python version string (``"3.12"``).  Defaults to the current interpreter.
    skip_installed:
        When True (default), skip packages that already have a variant this machine
        uses. When False, reinstall this machine's variant of every package.
    uv_cmd:
        Override the uv binary path.  Defaults to ``find_uv_bin()``.

    Returns:
    -------
    InstallReport
        What was new, what gained this machine's variant, and which other platforms
        the store already served.

    Raises:
    ------
    RezInstallError
        When any package could not be installed. Every other package is installed first.
    """
    uv = uv_cmd or find_uv_bin()
    if python_version is None:
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    resolved = resolve_full(
        packages,
        extra_index_url=extra_index_url,
        extra_flags=extra_flags,
        python_version=python_version,
        uv_cmd=uv,
    )
    resolved_versions = {pip_normalize(pkg.pip_name): pkg.version for pkg in resolved}

    report = InstallReport()
    failed: list[str] = []

    for pkg in resolved:
        label = f"{pkg.pip_name}=={pkg.version}"
        state = install_state(
            pkg.pip_name, pkg.version, packages_dir, python_version=python_version, backend=pkg.torch_backend
        )
        report.other_platforms |= _other_platforms(pkg.pip_name, pkg.version, packages_dir)
        if skip_installed and state == STATE_INSTALLED:
            logger.debug("[Rez][uv] skip (already installed for this machine): %s", label)
            report.already_installed.append(label)
            continue

        failure = _install_one(
            pkg,
            uv=uv,
            packages_dir=packages_dir,
            extra_index_url=extra_index_url,
            extra_flags=extra_flags,
            python_version=python_version,
            resolved_versions=resolved_versions,
        )
        if failure is not None:
            failed.append(f"{label} ({failure})")
        elif state == STATE_NEW:
            report.new.append(label)
        elif state == STATE_ADD_VARIANT:
            report.added_variant.append(label)
        else:
            report.rebuilt.append(label)

    logger.info(
        "[Rez][uv] complete — %d new, %d gained this machine's variant, %d rebuilt, %d already installed%s",
        len(report.new),
        len(report.added_variant),
        len(report.rebuilt),
        len(report.already_installed),
        f", {len(failed)} failed" if failed else "",
    )
    if failed:
        logger.warning("[Rez][uv] failed packages: %s", ", ".join(failed))
        raise RezInstallError(failed)
    return report
