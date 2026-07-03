from __future__ import annotations

import logging
import traceback
import types
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn, override
from cattrs.preconf.json import make_converter
from cattrs.strategies import include_subclasses, use_class_methods
from griptape.mixins.serializable_mixin import SerializableMixin
from pydantic import BaseModel

logger = logging.getLogger(__name__)

converter = make_converter()


# --- Unstructure hooks (serialization) ---

# SerializableMixin subclasses (BaseArtifact, BaseTool, Structure, etc.)
converter.register_unstructure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, SerializableMixin),
    lambda obj: obj.to_dict(),
)

# Pydantic BaseModel subclasses (WorkflowMetadata, WorkflowShape, etc.)
# mode="json" ensures all values are JSON-serializable (e.g. datetime -> ISO string)
converter.register_unstructure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, BaseModel),
    lambda obj: obj.model_dump(mode="json"),
)

# datetime subclasses (e.g. pendulum.DateTime from griptape) -> ISO format string
converter.register_unstructure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, datetime) and cls is not datetime,
    lambda obj: obj.isoformat(),
)


# Exception -> structured dict.
#
# The three dict keys are the wire form of ``ForwardedException``:
#   ``type``      -> ``ForwardedException.original_type``      -> ``[<type>]`` prefix
#   ``message``   -> ``ForwardedException.args[0]``            -> message body
#   ``traceback`` -> ``ForwardedException.original_traceback`` -> ``Worker traceback:`` block
# ``_structure_exception`` rebuilds the placeholder on the receiving side, and
# ``NodeExecutor._format_node_failure_message`` renders the prefix and block
# into the user-visible ``RuntimeError`` message.
def _unstructure_exception(obj: Exception) -> dict[str, Any]:
    if obj.__traceback__ is None:
        tb = None
    else:
        try:
            tb = "".join(traceback.format_exception(type(obj), obj, obj.__traceback__))
        except Exception:
            logger.debug("Failed to format traceback for %s", type(obj).__name__, exc_info=True)
            tb = None
    return {
        "type": f"{type(obj).__module__}.{type(obj).__qualname__}",
        "message": str(obj),
        "traceback": tb,
    }


converter.register_unstructure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, Exception),
    _unstructure_exception,
)

# Bare `type` references (e.g. provider_class: type)
converter.register_unstructure_hook(type, lambda t: f"{t.__module__}.{t.__qualname__}")


# --- Structure hooks (deserialization) ---

# The JSON preset strict mode rejects ints for float fields, but JSON has
# no distinction between int and float, so coerce int -> float on input.
converter.register_structure_hook(float, lambda v, _: float(v))

# Request payloads declare path-bearing fields as `Path` (e.g. project_path),
# but the wire form is always a string. Coerce so handlers can call .parent /
# Path arithmetic without first re-wrapping.
converter.register_structure_hook(Path, lambda v, _: Path(v))

# Union types composed entirely of JSON-primitive types (str, int, float, bool,
# dict, list, None). The JSON parser already produces the correct Python type,
# so no transformation is needed. This is required because cattrs cannot
# disambiguate certain combinations (e.g. dict | list) in a Union.
_JSON_PRIMITIVE_TYPES = frozenset({str, int, float, bool, dict, list, type(None)})


def _is_json_primitive_union(cls: Any) -> bool:
    origin = get_origin(cls)
    if origin is Union or origin is types.UnionType:
        return all(arg in _JSON_PRIMITIVE_TYPES for arg in get_args(cls))
    return False


converter.register_structure_hook_func(
    _is_json_primitive_union,
    lambda v, _: v,
)

# Pydantic BaseModel subclasses
converter.register_structure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, BaseModel),
    lambda obj, cls: cls.model_validate(obj),
)


# Exception <- structured dict.
#
# Rebuilds a ``ForwardedException`` on the receiving side because the
# worker-side class is rarely importable on the orchestrator. The
# ``original_type`` and ``original_traceback`` fields are read by
# ``NodeExecutor._format_node_failure_message`` to render the
# ``[<type>] ... Worker traceback: ...`` block in the orchestrator's
# user-visible ``RuntimeError`` message.
def _structure_exception(obj: Any, _cls: type) -> Exception:
    # Lazy import to avoid a circular dependency: base_events imports
    # from this module (event_converter is registered at import time
    # from base_events), so ForwardedException cannot be imported at
    # module load.
    from griptape_nodes.retained_mode.events.base_events import ForwardedException

    if not isinstance(obj, dict):
        return ForwardedException(str(obj))
    return ForwardedException(
        str(obj.get("message", "")),
        original_type=obj.get("type"),
        original_traceback=obj.get("traceback"),
    )


converter.register_structure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, Exception),
    _structure_exception,
)


# --- Hook factories for dataclasses ---
#
# Some event dataclasses have circular imports that force TYPE_CHECKING-only imports
# (e.g. flow_events <-> workflow_events). With `from __future__ import annotations`,
# cattrs' `get_type_hints()` can fail with NameError for those forward references.
# The factories below catch this and fall back to a simpler field-iteration approach.


def _fallback_unstructure(obj: Any) -> dict[str, Any]:
    """Fallback unstructure for dataclasses where get_type_hints() fails."""
    result = {}
    for f in dc_fields(obj):
        value = getattr(obj, f.name)
        try:
            result[f.name] = converter.unstructure(value)
        except Exception:
            logger.debug(
                "Failed to unstructure field '%s' (type=%s), using raw value",
                f.name,
                type(value).__name__,
                exc_info=True,
            )
            result[f.name] = value
    return result


def _make_fallback_structure_fn(cls: type) -> Any:
    """Fallback structure for dataclasses where get_type_hints() fails."""

    def structure_fn(data: dict[str, Any], _cls: type = cls) -> Any:
        init_fields = {f.name for f in dc_fields(_cls) if f.init}
        filtered = {k: v for k, v in data.items() if k in init_fields}
        return _cls(**filtered)

    return structure_fn


def _make_dataclass_unstructure_fn(cls: type) -> Any:
    """Generate an unstructure function that includes init=False fields."""
    try:
        return make_dict_unstructure_fn(cls, converter, _cattrs_include_init_false=True)
    except NameError:
        return _fallback_unstructure


def _make_dataclass_structure_fn(cls: type) -> Any:
    """Generate a structure function that omits init=False fields."""
    try:
        overrides = {}
        for f in dc_fields(cls):
            if not f.init:
                overrides[f.name] = override(omit=True)
        return make_dict_structure_fn(cls, converter, **overrides)
    except NameError:
        return _make_fallback_structure_fn(cls)


converter.register_unstructure_hook_factory(
    lambda cls: is_dataclass(cls) and isinstance(cls, type),
    _make_dataclass_unstructure_fn,
)

converter.register_structure_hook_factory(
    lambda cls: is_dataclass(cls) and isinstance(cls, type),
    _make_dataclass_structure_fn,
)

# --- Class-specific (un)structuring methods ---
#
# Classes that define `_cattrs_structure` (classmethod) and/or `_cattrs_unstructure`
# (instance method) use those for custom serialization.  Registered after the dataclass
# factories so that `use_class_methods` has higher priority (cattrs checks factories in
# reverse registration order), ensuring _cattrs_structure/_cattrs_unstructure take
# precedence over the generated dataclass code.
use_class_methods(converter, structure_method_name="_cattrs_structure", unstructure_method_name="_cattrs_unstructure")


def register_polymorphic_dataclass(cls: type) -> None:
    """Configure the converter to (un)structure ``cls`` as a union of itself and its subclasses.

    Without this, a field typed ``list[BaseClass]`` round-trips every entry
    as the base class and silently drops subclass-only fields. Call this
    once per polymorphic root, after every subclass has been declared at
    import time. Lives here so converter wiring stays in one place rather
    than each data module reaching into ``cattrs.strategies`` itself.

    Disambiguation is by unique field names; if a future subclass has no
    unique field, switch to a tagged-union strategy at this seam.
    """
    include_subclasses(cls, converter)


def safe_unstructure(obj: Any) -> Any:
    """Unstructure an arbitrary object into a JSON-serializable form.

    Wraps the cattrs converter with a fallback for dataclasses that tries
    each field individually, so a single bad field doesn't lose the entire
    object. Falls back to str() only as a last resort.
    """
    try:
        return converter.unstructure(obj)
    except Exception:
        logger.debug("Failed to unstructure object (type=%s), using fallback", type(obj).__name__, exc_info=True)
        if is_dataclass(obj) and not isinstance(obj, type):
            return _fallback_unstructure(obj)
        return str(obj)
