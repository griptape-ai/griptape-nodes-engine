"""Turn any parameter value into tagged plain data, and back.

Plain data (None, bool, int, float, str, lists, and dicts with text keys) encodes as itself.
Every other value becomes a dict whose ``$type`` key names its class. A dict-shaped state sits
beside ``$type``; any other state sits under ``$value``:

    {"$type": "griptape.artifacts.image_url_artifact:ImageUrlArtifact", "type": "ImageUrlArtifact", ...}
    {"$type": "builtins:tuple", "$value": [1, "b"]}

A class's state comes from the first adapter that claims it. A class can supply its own by
defining ``to_state()`` and a ``from_state(state)`` classmethod; other types can be covered with
``register_value_adapter``.

Decoding imports the module a ``$type`` names, and builds only classes an adapter claims. A value
this process cannot build, such as one whose class lives in a library another process loads,
decodes to an ``UndecodedValue`` that encodes back to exactly the data it came from, so it passes
through to a process that can build it.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import decimal
import enum
import hashlib
import inspect
import json
import logging
import math
import uuid
import weakref
from pathlib import Path, PurePath
from typing import Any, Protocol

import attrs
from griptape.mixins.serializable_mixin import SerializableMixin
from pydantic import BaseModel

from griptape_nodes.serialization.type_names import TypeNameError, resolve_type_name, type_name

TYPE_KEY = "$type"
VALUE_KEY = "$value"

type Value = Any
"""Any parameter value. Payload fields annotated with it cross the wire as tagged plain data."""

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class ValueEncodeError(TypeError):
    """A value has no plain-data form."""


logger = logging.getLogger("griptape_nodes")


class UndecodedValue(dict):
    """A value this process cannot build, kept as the plain data it arrived as.

    It encodes back to that same data, so passing it on loses nothing. It is a dict so code that
    reads artifact-shaped dicts keeps working with it.
    """

    def __init__(self, data: dict[str, Any], reason: str) -> None:
        super().__init__(data)
        self.reason = reason


class ValueAdapter(Protocol):
    """Converts instances of the classes it claims to and from a state of plain data or values."""

    def claims(self, cls: type) -> bool: ...

    def to_state(self, value: Any) -> Any: ...

    def from_state(self, cls: type, state: Any) -> Any: ...


def encode_value(value: Any) -> JsonValue:
    """Return ``value`` as plain data that ``decode_value`` turns back into an equal value.

    Raises:
        ValueEncodeError: ``value``, or something inside it, has no plain-data form.
    """
    return _encode(value, set())


def decode_value(data: Any) -> Any:
    """Rebuild the value ``encode_value`` produced ``data`` from.

    A tagged value this process cannot build comes back as an ``UndecodedValue``. Objects that are
    not plain data are already decoded and pass through unchanged.
    """
    if isinstance(data, list):
        return [decode_value(item) for item in data]
    if not isinstance(data, dict):
        return data
    if TYPE_KEY not in data:
        return {key: decode_value(item) for key, item in data.items()}
    return _decode_tagged(data)


def is_plain_data(value: Any) -> bool:
    """Whether ``value`` is already in the form ``encode_value`` returns: JSON types only."""
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(is_plain_data(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and is_plain_data(item) for key, item in value.items())
    return False


def value_key(encoded: JsonValue) -> str:
    """A key that is the same for every encoding of equal content, for pooling saved values."""
    return hashlib.sha256(_canonical_json(encoded).encode("utf-8")).hexdigest()[:32]


def register_value_adapter(adapter: ValueAdapter) -> None:
    """Consult ``adapter`` before every adapter registered earlier and every built-in one."""
    _adapters.insert(0, adapter)
    _adapter_cache.clear()


def _encode(value: Any, active: set[int]) -> JsonValue:
    cls = type(value)
    if value is None or cls in (bool, int, str):
        return value
    if cls is UndecodedValue:
        return dict(value)
    if cls is float:
        if math.isfinite(value):
            return value
        return _tagged(float, repr(value))
    if cls in (bytes, bytearray):
        return _tagged(cls, base64.b64encode(value).decode("ascii"))
    if id(value) in active:
        msg = f"A '{cls.__qualname__}' value contains itself."
        raise ValueEncodeError(msg)
    active.add(id(value))
    try:
        return _encode_compound(value, cls, active)
    finally:
        active.discard(id(value))


def _encode_compound(value: Any, cls: type, active: set[int]) -> JsonValue:  # noqa: PLR0911
    if cls is list:
        return [_encode(item, active) for item in value]
    if cls is dict:
        return _encode_dict(value, active)
    if cls is tuple:
        return _tagged(tuple, [_encode(item, active) for item in value])
    if cls in (set, frozenset):
        # Sorted by encoded form so an unchanged set always encodes the same way.
        items = [_encode(item, active) for item in value]
        return _tagged(cls, sorted(items, key=_canonical_json))
    if isinstance(value, Path):
        # Named as Path, not PosixPath or WindowsPath, so a file saved on one OS opens on another.
        return _tagged(Path, str(value))
    adapter = _adapter_for(cls)
    if adapter is not None:
        return _tagged(cls, _encode(_adapter_state(adapter, value), active))
    return _encode_as_base_type(value, cls, active)


def _encode_as_base_type(value: Any, cls: type, active: set[int]) -> JsonValue:
    """Like json.dumps, treat subclasses of plain-data types as their base type."""
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__index__(value)
    if isinstance(value, float):
        return _encode(float.__float__(value), active)
    if isinstance(value, list):
        return [_encode(item, active) for item in value]
    if isinstance(value, dict):
        return _encode_dict(value, active)
    msg = f"A '{cls.__qualname__}' value has no plain-data form."
    raise ValueEncodeError(msg)


def _encode_dict(value: dict, active: set[int]) -> JsonValue:
    if not all(isinstance(key, str) for key in value):
        # Pairs keep keys that are not text.
        return _tagged(dict, [[_encode(key, active), _encode(item, active)] for key, item in value.items()])
    encoded = {key: _encode(item, active) for key, item in value.items()}
    if TYPE_KEY not in value:
        return encoded
    # Wrapped, so a text "$type" key does not read as a tag. Encoded values held as data, such
    # as a saved workflow's value pool, stay readable this way.
    return _tagged(dict, encoded)


def _adapter_state(adapter: ValueAdapter, value: Any) -> Any:
    try:
        return adapter.to_state(value)
    except ValueEncodeError:
        raise
    except Exception as error:
        # Adapters run library code, which can raise anything.
        msg = f"A '{type(value).__qualname__}' value failed to produce its plain-data form: {error}"
        raise ValueEncodeError(msg) from error


def _tagged(cls: type, state: JsonValue) -> dict[str, JsonValue]:
    try:
        name = type_name(cls)
    except TypeNameError as error:
        raise ValueEncodeError(str(error)) from error
    if isinstance(state, dict) and TYPE_KEY not in state and VALUE_KEY not in state:
        return {TYPE_KEY: name, **state}
    return {TYPE_KEY: name, VALUE_KEY: state}


def _canonical_json(data: JsonValue) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _decode_tagged(data: dict[str, Any]) -> Any:
    name = data[TYPE_KEY]
    if not isinstance(name, str):
        return UndecodedValue(data, f"its type name is {name!r}, not text")
    try:
        cls = resolve_type_name(name)
    except TypeNameError as error:
        # Expected wherever a library's classes live in another process, so no warning.
        return UndecodedValue(data, str(error))
    if VALUE_KEY not in data:
        state = decode_value({key: item for key, item in data.items() if key != TYPE_KEY})
    elif cls is dict and isinstance(data[VALUE_KEY], dict):
        # A wrapped dict: its own "$type" key is data, not a tag.
        state = {key: decode_value(item) for key, item in data[VALUE_KEY].items()}
    else:
        state = decode_value(data[VALUE_KEY])
    builtin_decoder = _BUILTIN_DECODERS.get(cls)
    if builtin_decoder is not None:
        return _decode_builtin(builtin_decoder, data, state)
    adapter = _adapter_for(cls)
    if adapter is None:
        return UndecodedValue(data, f"'{name}' has no plain-data form in this process")
    try:
        return adapter.from_state(cls, state)
    except Exception as error:
        # Adapters run library code, which can raise anything.
        return _undecodable(data, f"a saved '{cls.__qualname__}' value could not be rebuilt: {error}")


def _decode_builtin(decoder: Any, data: dict[str, Any], state: Any) -> Any:
    try:
        return decoder(state)
    except (TypeError, ValueError, binascii.Error) as error:
        return _undecodable(data, f"a saved '{data[TYPE_KEY]}' value is malformed: {error}")


def _undecodable(data: dict[str, Any], reason: str) -> UndecodedValue:
    """Keep data that names a class this process has but does not fit it, as when a library changed."""
    logger.warning("Kept a saved value as plain data because %s.", reason)
    return UndecodedValue(data, reason)


def _decode_bytes(state: str) -> bytes:
    return base64.b64decode(state, validate=True)


def _decode_bytearray(state: str) -> bytearray:
    return bytearray(_decode_bytes(state))


def _decode_dict(state: dict | list[list[Any]]) -> dict:
    if isinstance(state, dict):
        return state
    return {key: item for key, item in state}  # noqa: C416 pairs arrive as two-item lists, not tuples


def _adapter_for(cls: type) -> ValueAdapter | None:
    if cls in _adapter_cache:
        return _adapter_cache[cls]
    found = next((adapter for adapter in _adapters if adapter.claims(cls)), None)
    _adapter_cache[cls] = found
    return found


def _is_classmethod(cls: type, name: str) -> bool:
    return isinstance(inspect.getattr_static(cls, name, None), classmethod)


class _StateMethodsAdapter:
    """Classes that define ``to_state()`` and a ``from_state(state)`` classmethod."""

    def claims(self, cls: type) -> bool:
        return callable(getattr(cls, "to_state", None)) and _is_classmethod(cls, "from_state")

    def to_state(self, value: Any) -> Any:
        return value.to_state()

    def from_state(self, cls: type, state: Any) -> Any:
        return cls.from_state(state)  # pyright: ignore[reportAttributeAccessIssue]


class _EnumAdapter:
    def claims(self, cls: type) -> bool:
        return issubclass(cls, enum.Enum)

    def to_state(self, value: enum.Enum) -> Any:
        return value.value

    def from_state(self, cls: type, state: Any) -> Any:
        return cls(state)


class _PathAdapter:
    def claims(self, cls: type) -> bool:
        return issubclass(cls, PurePath)

    def to_state(self, value: PurePath) -> str:
        return str(value)

    def from_state(self, cls: type, state: str) -> Any:
        return cls(state)


class _IsoFormatAdapter:
    """Dates, times, and datetimes, including subclasses such as pendulum's."""

    def claims(self, cls: type) -> bool:
        return issubclass(cls, (datetime.date, datetime.time))

    def to_state(self, value: datetime.date | datetime.time) -> str:
        return value.isoformat()

    def from_state(self, cls: type, state: str) -> Any:
        return cls.fromisoformat(state)  # pyright: ignore[reportAttributeAccessIssue]


class _TimedeltaAdapter:
    def claims(self, cls: type) -> bool:
        return issubclass(cls, datetime.timedelta)

    def to_state(self, value: datetime.timedelta) -> list[int]:
        return [value.days, value.seconds, value.microseconds]

    def from_state(self, cls: type, state: list[int]) -> Any:
        days, seconds, microseconds = state
        return cls(days=days, seconds=seconds, microseconds=microseconds)


class _TextFormAdapter:
    """Classes that print as text their constructor reads back."""

    def claims(self, cls: type) -> bool:
        return issubclass(cls, (uuid.UUID, decimal.Decimal))

    def to_state(self, value: uuid.UUID | decimal.Decimal) -> str:
        return str(value)

    def from_state(self, cls: type, state: str) -> Any:
        return cls(state)


class _NamedTupleAdapter:
    def claims(self, cls: type) -> bool:
        return issubclass(cls, tuple) and hasattr(cls, "_fields")

    def to_state(self, value: Any) -> dict[str, Any]:
        return value._asdict()

    def from_state(self, cls: type, state: dict[str, Any]) -> Any:
        return cls(**state)


class _PydanticAdapter:
    def claims(self, cls: type) -> bool:
        return issubclass(cls, BaseModel)

    def to_state(self, value: BaseModel) -> Any:
        return value.model_dump(mode="json")

    def from_state(self, cls: type, state: Any) -> Any:
        return cls.model_validate(state)  # pyright: ignore[reportAttributeAccessIssue]


class _GriptapeAdapter:
    """Griptape objects such as artifacts and rulesets."""

    def claims(self, cls: type) -> bool:
        return issubclass(cls, SerializableMixin)

    def to_state(self, value: SerializableMixin) -> Any:
        return value.to_dict()

    def from_state(self, cls: type[SerializableMixin], state: Any) -> Any:
        return cls.from_dict(state)


class _FieldsAdapter:
    """Dataclasses and attrs classes, by the fields their constructor takes."""

    def claims(self, cls: type) -> bool:
        return dataclasses.is_dataclass(cls) or attrs.has(cls)

    def to_state(self, value: Any) -> dict[str, Any]:
        if attrs.has(type(value)):
            return {field.alias: getattr(value, field.name) for field in attrs.fields(type(value)) if field.init}
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value) if field.init}

    def from_state(self, cls: type, state: dict[str, Any]) -> Any:
        return cls(**state)


_BUILTIN_DECODERS: dict[type, Any] = {
    tuple: tuple,
    set: set,
    frozenset: frozenset,
    bytes: _decode_bytes,
    bytearray: _decode_bytearray,
    dict: _decode_dict,
    float: float,
}

_adapters: list[ValueAdapter] = [
    _StateMethodsAdapter(),
    _EnumAdapter(),
    _PathAdapter(),
    _IsoFormatAdapter(),
    _TimedeltaAdapter(),
    _TextFormAdapter(),
    _NamedTupleAdapter(),
    _PydanticAdapter(),
    _GriptapeAdapter(),
    _FieldsAdapter(),
]

# Weak, so classes from a reloaded library file are not kept alive.
_adapter_cache: weakref.WeakKeyDictionary[type, ValueAdapter | None] = weakref.WeakKeyDictionary()
