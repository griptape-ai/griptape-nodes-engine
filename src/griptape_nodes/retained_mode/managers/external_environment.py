"""Hooks for running the engine inside an environment another tool has prepared.

A studio can start the engine inside an environment its own tools resolve (a package manager, a
container, a launcher). Two hooks let that environment, rather than the engine, decide what loads:

- `GTN_LIBRARY_PATHS` lists library manifests the environment provides. They register like
  `libraries_to_register` entries, ahead of them.
- `library.provisioned_by = "environment"` stops the engine from building virtual
  environments or downloading libraries, and limits loading to the libraries the environment lists.

Nothing here knows which tool prepared the environment.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.settings import (
    LibraryProvisioner,
    LibrarySettings,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

LIBRARY_PATHS_ENV_VAR = "GTN_LIBRARY_PATHS"
LIBRARY_SECTION_KEY = "library"


def read_provisioned_by(config_manager: ConfigManager) -> LibraryProvisioner:
    """The configured `library.provisioned_by`, read through the Settings validator.

    Running the field's own validator, rather than re-parsing the raw value, keeps this reader and
    the validator from drifting: an unrecognized value fails closed to 'environment' in both. Only
    this field is validated, so a bad value in another library setting does not change it.
    """
    library_section = config_manager.get_config_value(LIBRARY_SECTION_KEY, default={}) or {}
    if not isinstance(library_section, dict) or "provisioned_by" not in library_section:
        return LibrarySettings().provisioned_by
    return LibrarySettings.model_validate({"provisioned_by": library_section["provisioned_by"]}).provisioned_by


def provisioned_by_environment(config_manager: ConfigManager) -> bool:
    """Whether the environment, not the engine, provides libraries and their dependencies."""
    return read_provisioned_by(config_manager) is LibraryProvisioner.ENVIRONMENT


def library_paths_from_environment(environ: Mapping[str, str]) -> list[str]:
    """The library manifest paths listed in `GTN_LIBRARY_PATHS`, in order, blanks and repeats dropped."""
    raw_value = environ.get(LIBRARY_PATHS_ENV_VAR, "")
    entries = [entry.strip() for entry in raw_value.split(os.pathsep)]
    return list(dict.fromkeys(entry for entry in entries if entry))
