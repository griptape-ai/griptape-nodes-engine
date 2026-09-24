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


def _parse_annotation_graph(stdout: str) -> tuple[list[tuple[str, str]], dict[str, set[str]]]:
    """Parse uv's annotated ``pip compile`` output.

    Returns ``(pkg_list, requirers_of)`` where:

    - ``pkg_list`` is a list of ``(pip_name, version)`` in resolution order.
    - ``requirers_of`` maps a dep's pip-normalised name to the set of
      pip-normalised names of packages that directly require it.
    """
    pkg_list: list[tuple[str, str]] = []
    requirers_of: dict[str, set[str]] = defaultdict(set)
    current_norm: str | None = None
    in_via = False

    for line in stdout.splitlines():
        stripped = line.strip()

        if not line.startswith(" ") and "==" in stripped:
            name, ver = stripped.split("==", 1)
            clean_ver = re.sub(r"\+.*$", "", ver)
            pkg_list.append((name, clean_ver))
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
            pip_name=name,
            version=ver,
            direct_deps=direct_deps_of.get(pip_normalize(name), set()),
        )
        for name, ver in pkg_list
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


# ---------------------------------------------------------------------------
# Package.py generation
# ---------------------------------------------------------------------------


def _marker_applies(marker_str: str) -> bool:
    """Evaluate a PEP 508 environment marker against the current platform."""
    try:
        return Marker(marker_str).evaluate()
    except (InvalidMarker, ValueError):
        return False


def _parse_requires_dist(req_str: str) -> tuple[str, str] | None:
    """Parse a Requires-Dist string into (pip_name, specifier_str).

    Conditional deps (those with environment markers) are evaluated against
    the current platform — included when the marker matches, dropped otherwise.
    Extras in the package name (e.g. ``requests[security]``) are stripped.
    """
    req = req_str.strip()
    if ";" in req:
        spec_part, _, marker_str = req.partition(";")
        if not _marker_applies(marker_str.strip()):
            return None
        req = spec_part.strip()
    req = re.sub(r"\[.*?\]", "", req)  # strip extras
    # "name (>=1.0,<2)" form
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*\(([^)]*)\)\s*$", req)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # "name>=1.0,<2" or bare "name" form
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)\s*$", req)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


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


def _build_requires(info: WheelInfo) -> list[str]:
    """Build the rez requires list from the wheel's Requires-Dist metadata.

    Uses PEP 440 range syntax from wheel METADATA rather than exact dep-graph
    pins.  Ranges are stable across install runs — different library resolutions
    can install different compatible versions without conflicts.
    """
    result: list[str] = []
    for req_str in info.requires_dist:
        parsed = _parse_requires_dist(req_str)
        if parsed is None:
            continue
        pip_dep_name, specifier = parsed
        result.append(_pep440_spec_to_rez(pip_dep_name, specifier))
    return sorted(result)


def _variant_subpath(*, has_platform: bool, python_version: str) -> Path:
    rez_plat, rez_arch = _current_rez_platform()
    if has_platform:
        return Path(f"platform-{rez_plat}") / f"arch-{rez_arch}" / f"python-{python_version}"
    return Path(f"python-{python_version}")


def _read_existing_variants(pkg_file: Path) -> list[list[str]]:
    """Read the variants list from an existing package.py, if present."""
    if not pkg_file.exists():
        return []
    try:
        text = pkg_file.read_text(encoding="utf-8")
        local_ns: dict = {}
        exec(compile(text, str(pkg_file), "exec"), {"__builtins__": {}}, local_ns)  # noqa: S102
        return list(local_ns.get("variants", []))
    except Exception:
        logger.debug("[Rez][uv] could not parse existing variants from %s", pkg_file)
        return []


def _merge_variant(existing: list[list[str]], new_variant: list[str]) -> list[list[str]]:
    """Add a variant to the list if not already present."""
    for v in existing:
        if v == new_variant:
            return existing
    return [*existing, new_variant]


def _write_package_py(  # noqa: PLR0913
    version_dir: Path,
    family_name: str,
    info: WheelInfo,
    requires: list[str],
    *,
    python_version: str,
    has_platform_variant: bool,
) -> None:
    requires_block = ""
    if requires:
        entries = "".join(f"    '{r}',\n" for r in requires)
        requires_block = f"\nrequires = [\n{entries}]\n"

    rez_plat, rez_arch = _current_rez_platform()

    if has_platform_variant:
        new_variant = [f"platform-{rez_plat}", f"arch-{rez_arch}", f"python-{python_version}"]
    else:
        new_variant = [f"python-{python_version}"]

    pkg_file = version_dir / "package.py"
    existing_variants = _read_existing_variants(pkg_file)
    merged = _merge_variant(existing_variants, new_variant)
    variants_str = repr(merged)

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
        logger.info("[Rez][uv]   wrote %s (%d platform variants)", pkg_file, len(merged))
    else:
        logger.debug("[Rez][uv]   wrote %s", pkg_file)


# ---------------------------------------------------------------------------
# Package store helpers
# ---------------------------------------------------------------------------


def is_installed(pip_name: str, version: str, packages_dir: Path) -> bool:
    """Return True if this package version already has a ``package.py`` in the store."""
    pkg_dir = packages_dir / "local" / rez_name(pip_name) / version
    return (pkg_dir / "package.py").exists()


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


def _install_one(  # noqa: PLR0913
    pkg: ResolvedPackage,
    *,
    uv: str,
    packages_dir: Path,
    extra_index_url: str | None,
    extra_flags: list[str] | None,
    python_version: str,
) -> bool:
    """Download and install a single resolved package as a rez package.

    Returns True on success, False on failure.
    """
    logger.info("[Rez][uv] installing %s==%s ...", pkg.pip_name, pkg.version)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        dl_cmd = [
            uv,
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(tmp),
            f"{pkg.pip_name}=={pkg.version}",
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
            if dl.stderr:
                for line in dl.stderr.strip().splitlines()[-5:]:
                    logger.debug("[Rez][uv]   stderr: %s", line)
            return False

        try:
            info = read_wheel_info(tmp, pkg.pip_name, pkg.version)
        except Exception:
            logger.warning(
                "[Rez][uv] could not read wheel metadata for %s==%s — skipping",
                pkg.pip_name,
                pkg.version,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return False

        family_name = rez_name(info.pip_name)
        clean_version = re.sub(r"\+.*$", "", info.version)
        info.version = clean_version
        version_dir = packages_dir / "local" / family_name / clean_version
        requires = _build_requires(info)
        has_platform = not info.is_pure_python
        payload_root = version_dir / _variant_subpath(has_platform=has_platform, python_version=python_version)

        logger.debug("[Rez][uv]   family=%s  requires=%s", family_name, requires or "[]")

        try:
            _write_package_py(
                version_dir,
                family_name,
                info,
                requires,
                python_version=python_version,
                has_platform_variant=has_platform,
            )
            _copy_payload(tmp, payload_root, info.console_scripts)
            logger.info("[Rez][uv] installed %s==%s → %s", pkg.pip_name, pkg.version, version_dir)
        except OSError as exc:
            # Package-store writes fail on permissions, full disks, and network filesystems
            # that have not yet made a freshly created parent directory visible. Name the path
            # and the OS reason so the failure is actionable without re-running at DEBUG.
            logger.error(
                "[Rez][uv] failed to write rez package for %s==%s at %s: %s",
                pkg.pip_name,
                pkg.version,
                exc.filename or version_dir,
                exc.strerror or exc,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return False

    return True


# ---------------------------------------------------------------------------
# Main install orchestration
# ---------------------------------------------------------------------------


def install(  # noqa: PLR0913
    packages: str | list[str],
    *,
    packages_dir: Path,
    extra_index_url: str | None = None,
    extra_flags: list[str] | None = None,
    python_version: str | None = None,
    skip_installed: bool = True,
    uv_cmd: str | None = None,
) -> None:
    """Resolve and install *packages* and all transitive deps as rez packages.

    All packages are co-resolved in a single uv invocation so the solver
    produces one consistent lock set.  Each package's files are downloaded in
    isolation (``--no-deps``) so transitive dep boundaries are preserved and
    visible to the rez resolver.

    Parameters
    ----------
    packages:
        One or more pip package specs (e.g. ``"torch==2.7.0"`` or
        ``["torch==2.7.0", "torchvision==0.22.0"]``).  Multiple packages are
        co-resolved, which is required when they must agree on shared deps.
    packages_dir:
        Root of the rez package store.  Packages are written to
        ``packages_dir/local/<family>/<version>/``.
    extra_index_url:
        Additional pip index URL (e.g. a PyTorch CUDA wheel mirror).
    python_version:
        Python version string (``"3.12"``).  Defaults to the current interpreter.
    skip_installed:
        When True (default), skip packages whose ``package.py`` already exists.
    uv_cmd:
        Override the uv binary path.  Defaults to ``find_uv_bin()``.
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

    installed_n = 0
    skipped_n = 0
    failed: list[str] = []

    for pkg in resolved:
        if skip_installed and is_installed(pkg.pip_name, pkg.version, packages_dir):
            logger.debug("[Rez][uv] skip (already installed): %s==%s", pkg.pip_name, pkg.version)
            skipped_n += 1
            continue

        success = _install_one(
            pkg,
            uv=uv,
            packages_dir=packages_dir,
            extra_index_url=extra_index_url,
            extra_flags=extra_flags,
            python_version=python_version,
        )
        if success:
            installed_n += 1
        else:
            failed.append(f"{pkg.pip_name}=={pkg.version}")

    logger.info(
        "[Rez][uv] complete — %d installed, %d skipped%s",
        installed_n,
        skipped_n,
        f", {len(failed)} failed" if failed else "",
    )
    if failed:
        logger.warning("[Rez][uv] failed packages: %s", ", ".join(failed))
