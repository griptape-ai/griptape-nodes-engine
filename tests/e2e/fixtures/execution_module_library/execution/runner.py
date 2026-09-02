"""Execution-only module: imports the heavy dependency at module scope, normally.

This is the point of the boundary. `fakeexec` stands in for torch: it is declared as an execution
dependency, so it exists only in the worker's environment. A module-scope import here is correct
and safe, because the orchestrator never imports this file.
"""

from pathlib import Path

import fakeexec  # type: ignore[reportMissingImports]


def dependency_version() -> str:
    """Version of the execution dependency, read where it is actually importable."""
    return fakeexec.__version__


def load_and_run(device: str, weights: Path) -> str:
    """The shape a real model runner has: a device to place on, weights to load.

    Both arrive as ARGUMENTS. That is the whole arrangement -- the node asked the engine for the
    device and the asset path, so nothing here had to import a framework to find a GPU or invent a
    cache location, and nothing in the node had to import this module.
    """
    weight_files = sorted(path.name for path in weights.iterdir()) if weights.is_dir() else []
    return f"{fakeexec.__version__} on {device} with {weight_files}"
