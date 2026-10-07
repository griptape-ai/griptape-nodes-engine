"""Locations of the directories the engine keeps its own files in.

Each directory defaults to `<XDG base directory>/griptape_nodes`. A host application, such as the
desktop app, can move one by setting the matching environment variable to an absolute path. That
relocates only the engine, so child processes such as uv keep seeing the real XDG directories.
The variables name the final directory, so `griptape_nodes` is not appended to them.

A relative value is ignored, the same way the XDG specification treats relative base directories.
Values are read on every call rather than at import.
"""

from __future__ import annotations

import os
from pathlib import Path

from xdg_base_dirs import xdg_config_home, xdg_data_home, xdg_state_home

CONFIG_DIR_ENV_VAR = "GTN_ENGINE_CONFIG_DIR"
DATA_DIR_ENV_VAR = "GTN_ENGINE_DATA_DIR"
STATE_DIR_ENV_VAR = "GTN_ENGINE_STATE_DIR"

_ENGINE_DIR_NAME = "griptape_nodes"


def engine_config_dir() -> Path:
    """Directory holding the engine's user config file and `.env` file."""
    override = _absolute_override(CONFIG_DIR_ENV_VAR)
    if override is not None:
        return override
    return xdg_config_home() / _ENGINE_DIR_NAME


def engine_data_dir() -> Path:
    """Directory holding engine data such as libraries, the dedicated uv binary and ffmpeg."""
    override = _absolute_override(DATA_DIR_ENV_VAR)
    if override is not None:
        return override
    return xdg_data_home() / _ENGINE_DIR_NAME


def engine_state_dir() -> Path:
    """Directory holding engine state such as logs and session files."""
    override = _absolute_override(STATE_DIR_ENV_VAR)
    if override is not None:
        return override
    return xdg_state_home() / _ENGINE_DIR_NAME


def _absolute_override(env_var: str) -> Path | None:
    value = os.environ.get(env_var)
    if not value:
        return None

    override = Path(value)
    if not override.is_absolute():
        return None

    return override
