"""Utilities for working with the UV package manager."""

import sys
from pathlib import Path

import uv

from griptape_nodes.utils.engine_dirs import engine_data_dir


def find_uv_bin() -> str:
    """Find the uv binary, checking dedicated Griptape installation first, then system uv.

    Returns:
        Path to the uv binary to use
    """
    # Check for dedicated Griptape uv installation first
    dedicated_uv_path = engine_data_dir() / "bin" / "uv"
    if dedicated_uv_path.exists():
        return str(dedicated_uv_path)

    return uv.find_uv_bin()


def venv_python_path(venv_path: Path) -> Path:
    """Return the expected Python executable path inside a virtual environment."""
    if sys.platform.startswith("win"):
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def is_venv_functional(venv_path: Path) -> bool:
    """Check whether a directory contains a usable virtual environment.

    A venv is considered functional only if its directory exists, contains a
    ``pyvenv.cfg`` marker, and has a Python executable at the expected
    platform-specific location. The Python executable's target is not followed,
    because uv's own venv check works the same way: a missing or unresolvable
    interpreter on disk causes ``uv pip install --python <venv>/bin/python`` to
    fail with ``No virtual environment or system Python installation found``.
    """
    if not venv_path.is_dir():
        return False
    if not (venv_path / "pyvenv.cfg").is_file():
        return False
    return venv_python_path(venv_path).exists()
