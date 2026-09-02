"""Execution-only module: imports the heavy dependency at module scope, normally.

This is the point of the boundary. `fakeexec` stands in for torch: it is declared as an
execution dependency, so it exists only in the worker's environment. A module-scope import
here is correct and safe, because the orchestrator never imports this file.
"""

import fakeexec  # type: ignore[reportMissingImports]


def dependency_version() -> str:
    """Version of the execution dependency, read where it is actually importable."""
    return fakeexec.__version__
