"""Every event field annotated to carry parameter values keeps its annotation at runtime.

The converter falls back to untyped fields for a dataclass whose type hints do not resolve, so a
``Value`` field there silently loses its tags. Moving an import under ``TYPE_CHECKING`` is enough to
cause that, and nothing else fails.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import re
import typing

import pytest

# Loads the event modules in the order the engine does, so their deferred models finish building.
import griptape_nodes.retained_mode.engine  # noqa: F401
import griptape_nodes.retained_mode.events as events_package

_VALUE_ANNOTATION = re.compile(r"\b(Value|DisplayValue|ElementDocument)\b")
# A script that builds schemas on import, not an event module.
_NOT_EVENT_MODULES = frozenset({"generate_request_payload_schemas"})


def _dataclasses_with_value_fields() -> list[type]:
    found = []
    for module_info in pkgutil.iter_modules(events_package.__path__):
        if module_info.name in _NOT_EVENT_MODULES:
            continue
        module = importlib.import_module(f"{events_package.__name__}.{module_info.name}")
        for candidate in vars(module).values():
            if not (isinstance(candidate, type) and dataclasses.is_dataclass(candidate)):
                continue
            if candidate.__module__ != module.__name__:
                continue
            annotations = inspect.get_annotations(candidate)
            if any(_VALUE_ANNOTATION.search(str(annotation)) for annotation in annotations.values()):
                found.append(candidate)
    return found


@pytest.mark.parametrize("cls", _dataclasses_with_value_fields(), ids=lambda cls: cls.__qualname__)
def test_value_fields_resolve_at_runtime(cls: type) -> None:
    """A field type that does not resolve would send this payload's values untagged."""
    typing.get_type_hints(cls)


def test_value_fields_are_found() -> None:
    """Guards the search, so a broken search cannot pass by finding nothing."""
    assert len(_dataclasses_with_value_fields()) > 10  # noqa: PLR2004
