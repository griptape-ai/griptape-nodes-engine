"""Hooks for running the engine inside an environment another tool has prepared.

A studio can start the engine inside an environment its own tools resolve (a package manager, a
container, a launcher). Two hooks let that environment, rather than the engine, decide what loads:

- `GTN_LIBRARY_PATHS` lists library manifests the environment provides. They register like
  `libraries_to_register` entries, ahead of them.
- `library.dependency_source = "environment"` stops the engine from building virtual
  environments or downloading libraries, and limits loading to the libraries the environment lists.

Nothing here knows which tool prepared the environment.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_DEPENDENCY_SOURCE_KEY,
    LibraryDependencySource,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

LIBRARY_PATHS_ENV_VAR = "GTN_LIBRARY_PATHS"


def read_dependency_source(config_manager: ConfigManager) -> LibraryDependencySource:
    """The configured `library.dependency_source`, in any letter case.

    The merged config keeps the raw value a config file carries, so it is normalized here. A value
    that is not a known source reads as 'venv'; the Settings validator has already warned about it.
    """
    raw_value = config_manager.get_config_value(
        LIBRARY_DEPENDENCY_SOURCE_KEY, default=LibraryDependencySource.VENV.value
    )
    if isinstance(raw_value, LibraryDependencySource):
        return raw_value
    if not isinstance(raw_value, str):
        return LibraryDependencySource.VENV
    try:
        return LibraryDependencySource(raw_value.strip().lower())
    except ValueError:
        return LibraryDependencySource.VENV


def uses_environment_dependencies(config_manager: ConfigManager) -> bool:
    """Whether the environment, not the engine, provides libraries and their dependencies."""
    return read_dependency_source(config_manager) is LibraryDependencySource.ENVIRONMENT


def library_paths_from_environment(environ: Mapping[str, str]) -> list[str]:
    """The library manifest paths listed in `GTN_LIBRARY_PATHS`, in order, blanks and repeats dropped."""
    raw_value = environ.get(LIBRARY_PATHS_ENV_VAR, "")
    entries = [entry.strip() for entry in raw_value.split(os.pathsep)]
    return list(dict.fromkeys(entry for entry in entries if entry))
