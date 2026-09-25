from __future__ import annotations

import json
import logging
import traceback
import types
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn, make_hetero_tuple_unstructure_fn, override
from cattrs.preconf.json import make_converter
from cattrs.strategies import include_subclasses, use_class_methods
from griptape.mixins.serializable_mixin import SerializableMixin
from pydantic import BaseModel

from griptape_nodes.common.macro_parser.core import ParsedMacro
from griptape_nodes.serialization.type_names import resolve_type_name, type_name
from griptape_nodes.serialization.values import (
    DisplayValue,
    Value,
    ValueEncodeError,
    decode_value,
    encode_for_display,
    encode_value,
)

if TYPE_CHECKING:
    from cattrs import Converter

logger = logging.getLogger(__name__)

converter = make_converter()


# --- Unstructure hooks (serialization) ---

# Fields annotated `Value` carry any parameter value, as tagged plain data.
converter.register_unstructure_hook(Value, encode_value)
converter.register_unstructure_hook(DisplayValue, encode_for_display)


# Griptape objects (artifacts, rulesets, drivers) are parameter values, so they cross only in fields
# typed `Value` or `DisplayValue`. Anywhere else cattrs would walk their attrs fields and fail on a
# type hint griptape imports only for type checking, with an error that names neither the object
# nor the field.
def _refuse_griptape_object(obj: SerializableMixin) -> Any:
    msg = f"A '{type(obj).__qualname__}' value is sent only in a field that carries parameter values."
    raise ValueEncodeError(msg)


converter.register_unstructure_hook_func(
    lambda cls: isinstance(cls, type) and issubclass(cls, SerializableMixin),
    _refuse_griptape_object,
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

type ElementDocument = dict[str, Any]
"""A node element and its children, as the editor sees them. Parameter values sit under
``value``, ``default_value``, and ``element_id_to_value``, and cross the wire as display values."""

_ELEMENT_VALUE_KEYS = frozenset({"value", "default_value"})


def _unstructure_element_document(document: dict[str, Any]) -> dict[str, Any]:
    return {key: _unstructure_element_entry(key, item) for key, item in document.items()}


def _unstructure_element_entry(key: str, item: Any) -> Any:
    if key in _ELEMENT_VALUE_KEYS:
        return encode_for_display(item)
    if key == "element_id_to_value":
        return {element_id: encode_for_display(value) for element_id, value in item.items()}
    if key == "children" and isinstance(item, list):
        return [_unstructure_element_document(child) for child in item]
    return converter.unstructure(item)


def _structure_element_document(document: dict[str, Any], _cls: Any) -> dict[str, Any]:
    return {key: _structure_element_entry(key, item) for key, item in document.items()}


def _structure_element_entry(key: str, item: Any) -> Any:
    if key in _ELEMENT_VALUE_KEYS:
        return decode_value(item)
    if key == "element_id_to_value":
        return {element_id: decode_value(value) for element_id, value in item.items()}
    if key == "children" and isinstance(item, list):
        return [_structure_element_document(child, None) for child in item]
    return item


converter.register_unstructure_hook(ElementDocument, _unstructure_element_document)
converter.register_structure_hook(ElementDocument, _structure_element_document)

# Bare `type` references (e.g. provider_class: type), named the way the value codec names classes.
converter.register_unstructure_hook(type, type_name)

# ParsedMacro -> its template string. `segments` is parsed from the template by __post_init__ and
# never set by a caller, so the template is the entire value: sending the segments would send a
# derived copy that the receiving side has to rebuild anyway. Without this, cattrs has no hook for
# the dataclass and passes it through untouched, so the failure lands in json.dumps instead.
converter.register_unstructure_hook(ParsedMacro, lambda macro: macro.template)


# --- Structure hooks (deserialization) ---

converter.register_structure_hook(Value, lambda data, _: decode_value(data))
converter.register_structure_hook(DisplayValue, lambda data, _: decode_value(data))

converter.register_structure_hook(ParsedMacro, lambda template, _: ParsedMacro(template))

converter.register_structure_hook(type, lambda name, _: resolve_type_name(name))

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


# Unions of enums (e.g. `SequenceScanFailureReason | FileIOFailureReason`) arrive as a bare member
# value, which cattrs cannot attribute to one enum. The first enum with that value claims it.
def _enum_union_members(cls: Any) -> list[type[Enum]] | None:
    origin = get_origin(cls)
    if origin is not Union and origin is not types.UnionType:
        return None
    args = [arg for arg in get_args(cls) if arg is not type(None)]
    if not args or not all(isinstance(arg, type) and issubclass(arg, Enum) for arg in args):
        return None
    return args


def _structure_enum_union(value: Any, cls: Any) -> Enum | None:
    if value is None and type(None) in get_args(cls):
        return None
    for enum_cls in _enum_union_members(cls) or []:
        try:
            return enum_cls(value)
        except ValueError:
            continue
    msg = f"{value!r} is not a member of any of {cls}."
    raise ValueError(msg)


converter.register_structure_hook_func(lambda cls: _enum_union_members(cls) is not None, _structure_enum_union)

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
    # from this module (the converter is registered at import time
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


# --- Hook factories for dataclasses and NamedTuples ---
#
# Each factory takes the converter it builds a hook for, so a copy of this converter builds hooks
# that recurse through the copy.
#
# Some event dataclasses have circular imports that force TYPE_CHECKING-only imports
# (e.g. library_events -> library_manager -> library_events). With `from __future__ import annotations`,
# cattrs' `get_type_hints()` can fail with NameError for those forward references.
# The dataclass factories catch this and fall back to a simpler field-iteration approach.


def _make_fallback_unstructure_fn(conv: Converter) -> Any:
    """Fallback unstructure for dataclasses where get_type_hints() fails: each field by its runtime type."""

    def unstructure_fn(obj: Any) -> dict[str, Any]:
        return {f.name: conv.unstructure(getattr(obj, f.name)) for f in dc_fields(obj)}

    return unstructure_fn


def _make_fallback_structure_fn(cls: type) -> Any:
    """Fallback structure for dataclasses where get_type_hints() fails."""

    def structure_fn(data: dict[str, Any], _cls: type = cls) -> Any:
        init_fields = {f.name for f in dc_fields(_cls) if f.init}
        filtered = {k: v for k, v in data.items() if k in init_fields}
        return _cls(**filtered)

    return structure_fn


def _make_dataclass_unstructure_fn(cls: type, conv: Converter) -> Any:
    """Generate an unstructure function that includes init=False fields."""
    try:
        return make_dict_unstructure_fn(cls, conv, _cattrs_include_init_false=True)
    except NameError:
        return _make_fallback_unstructure_fn(conv)


def _make_dataclass_structure_fn(cls: type, conv: Converter) -> Any:
    """Generate a structure function that omits init=False fields."""
    try:
        overrides = {}
        for f in dc_fields(cls):
            if not f.init:
                overrides[f.name] = override(omit=True)
        return make_dict_structure_fn(cls, conv, **overrides)
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


# NamedTuples, by their resolved field types. cattrs' own hooks read the raw annotations, which
# `from __future__ import annotations` leaves as text, so a field typed `str` fails to structure.
def _is_namedtuple(cls: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, tuple) and hasattr(cls, "_fields")


def _make_namedtuple_unstructure_fn(cls: type, conv: Converter) -> Any:
    field_types = tuple(get_type_hints(cls).values())
    return make_hetero_tuple_unstructure_fn(cls, conv, unstructure_to=tuple, type_args=field_types)


def _make_namedtuple_structure_fn(cls: type, conv: Converter) -> Any:
    fields_tuple = tuple[tuple(get_type_hints(cls).values())]
    structure_fields = conv.get_structure_hook(fields_tuple)
    return lambda data, _: cls(*structure_fields(data, fields_tuple))


converter.register_unstructure_hook_factory(_is_namedtuple, _make_namedtuple_unstructure_fn)
converter.register_structure_hook_factory(_is_namedtuple, _make_namedtuple_structure_fn)

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


def dump_json(data: Any, **kwargs: Any) -> str:
    """Write the converter's output as JSON text.

    Raises:
        ValueEncodeError: ``data`` holds a value with no JSON form. The converter passes objects it
            has no hook for through unchanged, so this is where they surface.
    """
    return json.dumps(data, default=_refuse_json_value, **kwargs)


# Passed explicitly: griptape swaps `JSONEncoder.default` process-wide for one that sends any object
# with a `to_dict()` through it, and fails on the rest without naming their type.
def _refuse_json_value(obj: Any) -> Any:
    msg = f"A '{type(obj).__qualname__}' value has no plain-data form."
    raise ValueEncodeError(msg)
