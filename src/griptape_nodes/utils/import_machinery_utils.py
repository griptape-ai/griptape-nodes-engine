"""Guards against advanced-library hooks damaging Python's import machinery.

Advanced libraries commonly clear entries out of `sys.modules` in their
`before_library_nodes_loaded` hook, so that heavy dependencies (`transformers`,
`huggingface_hub`) get re-imported from the library's own venv instead of the
engine's. That is fine for third-party packages, but `importlib._bootstrap` is
the running import system itself: its source file contains no `import sys`,
because the interpreter injects `sys` into the frozen module's namespace at
startup. Evicting it means the next import re-executes the file from disk and
produces a copy with no `sys` global, after which every `import_module()` call
in the process raises `NameError: name 'sys' is not defined`.

The damage is process-wide and outlives the hook that caused it, so without a
check here the failures surface later against whichever library or node module
imports next -- never against the library actually responsible.
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

# The submodules of `importlib` that the interpreter loads as frozen modules and that rely on
# interpreter-injected globals. Re-executing any of these from source yields a module that is
# missing those globals, which breaks importing for the rest of the process.
_FROZEN_IMPORT_MODULES = ("importlib._bootstrap", "importlib._bootstrap_external")

# `__spec__.origin` for a module the interpreter froze into the binary. A module re-executed from
# source has a filesystem path here instead.
_FROZEN_ORIGIN = "frozen"


class ImportMachinerySnapshot(NamedTuple):
    """The frozen import modules as they existed before a hook ran.

    Attributes:
        modules: Maps module name to the frozen module object that was in `sys.modules`. Names
            absent from `sys.modules` at snapshot time are omitted rather than stored as None,
            so restoring never invents an entry that was not there to begin with.
    """

    modules: dict[str, ModuleType]


def snapshot_import_machinery() -> ImportMachinerySnapshot:
    """Record the frozen import modules so damage can be detected and undone afterwards.

    Returns:
        A snapshot to hand to `restore_import_machinery`.
    """
    modules = {}
    for module_name in _FROZEN_IMPORT_MODULES:
        module = sys.modules.get(module_name)
        if module is not None:
            modules[module_name] = module

    return ImportMachinerySnapshot(modules=modules)


def restore_import_machinery(snapshot: ImportMachinerySnapshot) -> list[str]:
    """Put the frozen import modules back if something replaced or removed them.

    Repairing in place is worth doing rather than only reporting: the engine has a valid
    reference to the frozen module, and reinstating it makes imports work again for the rest
    of the session instead of requiring the user to restart the engine.

    Args:
        snapshot: The snapshot taken before the code that may have caused damage.

    Returns:
        The names of the modules that had to be restored, empty if the import machinery was
        left intact. Callers use a non-empty list to attribute the damage to its cause.
    """
    restored = []
    for module_name, frozen_module in snapshot.modules.items():
        current_module = sys.modules.get(module_name)
        if current_module is frozen_module:
            continue

        # Both halves matter. `sys.modules` is what future imports consult, and the attribute on
        # the parent package is what already-executed code like `importlib.import_module` reads
        # via `_bootstrap._gcd_import`; re-importing the submodule rebinds the latter too.
        sys.modules[module_name] = frozen_module
        _, _, attribute_name = module_name.partition("importlib.")
        setattr(importlib, attribute_name, frozen_module)
        restored.append(module_name)

    return restored


def find_unfrozen_import_modules() -> list[str]:
    """Find frozen import modules that have been re-executed from source.

    Unlike `restore_import_machinery` this needs no prior snapshot, so it can report on damage
    that predates the caller.

    Returns:
        The names of the affected modules, empty if the import machinery is intact.
    """
    unfrozen = []
    for module_name in _FROZEN_IMPORT_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue

        spec = getattr(module, "__spec__", None)
        if spec is not None and spec.origin != _FROZEN_ORIGIN:
            unfrozen.append(module_name)

    return unfrozen
