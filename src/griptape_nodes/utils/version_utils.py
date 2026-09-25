"""Version utilities for Griptape Nodes."""

from __future__ import annotations

import importlib.metadata
import json
import sysconfig
from pathlib import Path
from typing import Literal, NamedTuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion
from packaging.version import Version as PackagingVersion
from rich.console import Console

console = Console()

ENGINE_PACKAGE_NAME = "griptape-nodes-engine"

engine_version = importlib.metadata.version(ENGINE_PACKAGE_NAME)


class ShadowedPackage(NamedTuple):
    """A package a library environment supplies at an older version than the engine's own."""

    name: str
    library_version: str
    engine_version: str


def engine_version_failure_detail(spec_string: str | None) -> str | None:
    """Return a failure detail when the running engine fails `spec_string`, else None.

    A PEP 440 compare of the running engine against the given specifier. `None`
    means no constraint (no engine_version declared). A malformed spec or engine
    version is itself a failure detail rather than a raise, so callers (the
    activation-time gate and the read-only project-list preflight) can surface
    the same message without crashing.
    """
    if spec_string is None:
        return None

    try:
        specifier_set = SpecifierSet(spec_string)
    except InvalidSpecifier:
        return f"Config pins engine version '{spec_string}', which is not a valid PEP 440 specifier (e.g. '>=0.5,<0.6')"

    try:
        current_version = PackagingVersion(engine_version)
    except InvalidVersion:
        return (
            f"Config pins engine version '{spec_string}' but the running engine "
            f"version '{engine_version}' is not a valid PEP 440 version"
        )

    if current_version not in specifier_set:
        return f"Config requires engine version '{spec_string}' but the running engine is '{engine_version}'"

    return None


def get_current_version() -> str:
    """Returns the current version of the Griptape Nodes package."""
    return f"v{engine_version}"


def get_install_source(
    package_name: str = ENGINE_PACKAGE_NAME,
) -> tuple[Literal["git", "file", "pypi", "unknown"], str | None]:
    """Determines the install source of the given Griptape Nodes package.

    Searches for the dist-info in the same site-packages directory as the
    running code to correctly identify the source when multiple installations
    of the package exist across different environments on sys.path.

    Args:
        package_name: Distribution name to inspect (defaults to the engine).

    Returns:
        tuple: A tuple containing the install source and commit ID (if applicable).
    """
    package_aliases = {package_name.lower(), package_name.lower().replace("-", "_")}
    # Search for the dist-info in the same directory as this running module
    # (i.e., the site-packages containing the code that is actually executing).
    # This avoids picking up a different installation that appears earlier in
    # sys.path when multiple environments coexist (e.g., a uv tool install
    # alongside a local project install).
    code_site_packages = next(
        (p for p in Path(__file__).parents if p.name == "site-packages"),
        None,
    )
    dist = None
    if code_site_packages is not None:
        dist = next(
            (
                d
                for d in importlib.metadata.distributions(path=[str(code_site_packages)])
                if d.metadata.get("Name", "").lower() in package_aliases
            ),
            None,
        )

    # Fall back for editable installs where __file__ is in the source tree
    # rather than site-packages, so the dist-info won't be found above.
    if dist is None:
        try:
            dist = importlib.metadata.distribution(package_name)
        except importlib.metadata.PackageNotFoundError:
            return "unknown", None

    direct_url_text = dist.read_text("direct_url.json")
    # installing from pypi doesn't have a direct_url.json file
    if direct_url_text is None:
        return "pypi", None

    direct_url_info = json.loads(direct_url_text)
    url = direct_url_info.get("url")
    if url and url.startswith("file://"):
        return "file", None
    if "vcs_info" in direct_url_info:
        commit_id = direct_url_info["vcs_info"].get("commit_id")
        return "git", commit_id[:7] if commit_id else None
    # direct_url.json exists but matches no known pattern (e.g., direct HTTP tarball)
    return "unknown", None


def format_version_string(version: str, package_name: str = ENGINE_PACKAGE_NAME) -> str:
    """Format a version string with its install source and optional commit ID.

    Package-agnostic: pass any distribution name to describe a package other than
    the engine (e.g. the application layer installed on top of it).

    Format: v1.2.3 (source) or v1.2.3 (source - commit_id)
    """
    source, commit_id = get_install_source(package_name)
    if commit_id is None:
        return f"{version} ({source})"
    return f"{version} ({source} - {commit_id})"


def get_complete_version_string() -> str:
    """Returns the complete engine version string including install source and commit ID.

    Format: v1.2.3 (source) or v1.2.3 (source - commit_id)

    Returns:
        Complete version string with source and commit info.
    """
    return format_version_string(get_current_version(), ENGINE_PACKAGE_NAME)


def engine_package_versions() -> dict[str, str]:
    """Return the canonical name and installed version of every package in the engine's environment.

    Scoped to the running interpreter's site-packages rather than `sys.path`, which carries library
    environments spliced ahead of the engine's own: a path-wide scan reports those versions as the
    engine's, so the floors below would name the very copies they exist to displace.

    The engine's own distribution is excluded: every library declares it, and the copy in a library
    environment is frequently someone's editable checkout, which a floor would replace with a wheel.
    """
    purelib = sysconfig.get_path("purelib")
    platlib = sysconfig.get_path("platlib")
    search_paths = [purelib] if purelib == platlib else [purelib, platlib]

    engine_name = canonicalize_name(ENGINE_PACKAGE_NAME)
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=search_paths):
        raw_name = distribution.metadata.get("Name")
        if not raw_name or not distribution.version:
            continue
        name = canonicalize_name(raw_name)
        if name == engine_name:
            continue
        # PEP 610 records this for a git or local install only, and the version it reports is not a
        # version any index has to carry, so a floor on it can make a valid resolution impossible.
        if distribution.read_text("direct_url.json") is not None:
            continue
        versions[name] = distribution.version

    return versions


def engine_package_floors() -> tuple[str, ...]:
    """Return a `name>=version` floor for every package in the engine's own environment.

    Passed as constraints to a library's dependency install. A library environment precedes the
    engine's own on the import path, so a package the library resolves BELOW the engine's version
    is the one engine code binds. Floors let a library resolve newer, never older.
    """
    return tuple(f"{name}>={version}" for name, version in sorted(engine_package_versions().items()))


def packages_shadowing_the_engine(site_packages_paths: list[Path]) -> tuple[ShadowedPackage, ...]:
    """Return the packages in the given directories that are older than the engine's own copy.

    These are what the engine binds once such a directory precedes its own on the import path, so
    they are the versions engine code runs against rather than the ones it was tested with.

    Reported rather than prevented. Installing under `engine_package_floors` keeps most of them
    from arriving, but a library whose dependencies cannot satisfy a floor installs without them,
    and a package left behind by an earlier install is outside the resolution a later one
    constrains.
    """
    engine_versions = engine_package_versions()
    shadowed: dict[str, ShadowedPackage] = {}
    for site_packages in site_packages_paths:
        for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
            raw_name = distribution.metadata.get("Name")
            if not raw_name or not distribution.version:
                continue
            name = canonicalize_name(raw_name)
            engine_version = engine_versions.get(name)
            if engine_version is None:
                continue
            try:
                library_version = PackagingVersion(distribution.version)
                if library_version >= PackagingVersion(engine_version):
                    continue
            except InvalidVersion:
                # Nothing comparable, so nothing to report.
                continue
            # A library's two environments resolve separately and can hold different older copies
            # of the same package. The oldest is the one worth naming.
            already_found = shadowed.get(name)
            if already_found is not None and PackagingVersion(already_found.library_version) <= library_version:
                continue
            shadowed[name] = ShadowedPackage(
                name=name, library_version=distribution.version, engine_version=engine_version
            )

    return tuple(sorted(shadowed.values()))
