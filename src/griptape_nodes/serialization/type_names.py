"""Names that identify a class across processes, and the reverse lookup.

A type name is ``"<module>:<qualified name>"``, for example
``"griptape.artifacts.image_url_artifact:ImageUrlArtifact"``. Node library files load under a
per-process module name (``gtn_dynamic_module_...``) that no other process can import, so a
class from one is named by the stable alias its library registers instead.

The stable-name table is process-global because the module table it describes, ``sys.modules``,
is too.
"""

from __future__ import annotations

import importlib
import sys

DYNAMIC_MODULE_PREFIX = "gtn_dynamic_module_"

_stable_module_names: dict[str, str] = {}


class TypeNameError(Exception):
    """A class cannot be named, or a name cannot be resolved to a class."""


def register_stable_module_name(dynamic_module_name: str, stable_module_name: str) -> None:
    """Name classes from ``dynamic_module_name`` by ``stable_module_name`` from now on."""
    _stable_module_names[dynamic_module_name] = stable_module_name


def forget_stable_module_name(dynamic_module_name: str) -> None:
    """Undo ``register_stable_module_name``, as when a library unloads."""
    _stable_module_names.pop(dynamic_module_name, None)


def is_dynamic_module_name(module_name: str) -> bool:
    """True for the per-process module names node library files load under."""
    return module_name.startswith(DYNAMIC_MODULE_PREFIX)


def stable_module_name(module_name: str) -> str | None:
    """Return the name other processes can import ``module_name`` by, or None if there is none."""
    if not is_dynamic_module_name(module_name):
        return module_name
    return _stable_module_names.get(module_name)


def type_name(cls: type) -> str:
    """Return the name ``resolve_type_name`` turns back into ``cls``."""
    qualname = cls.__qualname__
    if "<locals>" in qualname:
        msg = f"'{qualname}' is defined inside a function, so no other process can find it."
        raise TypeNameError(msg)
    module_name = stable_module_name(cls.__module__)
    if module_name is None:
        msg = f"'{qualname}' comes from a library file that has no stable module name."
        raise TypeNameError(msg)
    return f"{module_name}:{qualname}"


def resolve_type_name(name: str) -> type:
    """Import the class ``name`` refers to."""
    module_name, separator, qualname = name.partition(":")
    if not separator or not module_name or not qualname:
        msg = f"'{name}' is not a type name."
        raise TypeNameError(msg)
    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            msg = f"'{qualname}' needs module '{module_name}', which failed to import: {error}"
            raise TypeNameError(msg) from error
    resolved: object = module
    for attribute in qualname.split("."):
        resolved = getattr(resolved, attribute, None)
        if resolved is None:
            msg = f"Module '{module_name}' has no '{qualname}'."
            raise TypeNameError(msg)
    if not isinstance(resolved, type):
        msg = f"'{name}' does not name a class."
        raise TypeNameError(msg)
    return resolved
