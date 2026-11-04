"""Make a launcher's import paths available without letting them shadow the engine's packages.

`PYTHONPATH` is searched before site-packages, so an environment that sets it (Rez, Conda, a custom
launcher) can replace the packages the engine was installed with and break it on import. A launcher
that runs the engine on its own interpreter can remove `PYTHONPATH` and pass it here instead. These
entries go to the end of `sys.path`: modules the engine does not ship stay importable, and the
engine's packages always win.

Stdlib only, because `griptape_nodes/__init__.py` runs this before anything else is imported.
"""

import os
import sys
from collections.abc import Mapping

APPEND_PYTHONPATH_ENV_VAR = "GTN_APPEND_PYTHONPATH"


def append_python_path_from_environment(environ: Mapping[str, str], search_path: list[str]) -> None:
    """Append each entry of `GTN_APPEND_PYTHONPATH` to `search_path`, skipping ones already on it.

    Entries are separated like `PATH` (`:`, or `;` on Windows). Relative entries are made absolute
    against the working directory, as Python does for `PYTHONPATH`, so a later `chdir` does not
    change what they point at.
    """
    value = environ.get(APPEND_PYTHONPATH_ENV_VAR)
    if not value:
        return
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        absolute_entry = os.path.abspath(entry)  # noqa: PTH100 - sys.path holds strings
        if absolute_entry in search_path:
            continue
        search_path.append(absolute_entry)


def append_python_path() -> None:
    """Apply `GTN_APPEND_PYTHONPATH` to this process's `sys.path`."""
    append_python_path_from_environment(os.environ, sys.path)
