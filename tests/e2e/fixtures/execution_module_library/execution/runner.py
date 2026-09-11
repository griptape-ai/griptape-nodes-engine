"""Execution-only module: imports the heavy dependency at module scope, normally.

This is the point of the boundary. `fakeexec` stands in for torch: it is declared as an execution
dependency, so it exists only in the worker's environment. A module-scope import here is correct
and safe, because the orchestrator never imports this file.
"""

import fakeexec  # type: ignore[reportMissingImports]


def dependency_version() -> str:
    """Version of the execution dependency, read where it is actually importable."""
    return fakeexec.__version__


def load_and_run(device: str) -> str:
    """The shape a real model runner has: a device to place the model on.

    It arrives as an ARGUMENT. That is the whole arrangement -- the node asked the engine which
    device to use, so nothing here had to import a framework to find a GPU, and nothing in the node
    had to import this module.
    """
    return f"{fakeexec.__version__} on {device}"
