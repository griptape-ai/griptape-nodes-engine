"""Turn any parameter value into tagged plain data, and back.

Plain data (None, bool, int, float, str, lists, and dicts with text keys) encodes as itself.
Every other value becomes a dict whose ``$type`` key names its class. A dict-shaped state sits
beside ``$type``; any other state sits under ``$value``:

    {"$type": "griptape.artifacts.image_url_artifact:ImageUrlArtifact", "type": "ImageUrlArtifact", ...}
    {"$type": "builtins:tuple", "$value": [1, "b"]}

A class's state comes from its codec: a pair of functions to and from plain data. Library authors
add codecs with ``register_value_codec``; the engine's built-in codecs cover enums, paths, dates,
pydantic models, dataclasses, attrs classes, and griptape objects.

Decoding imports a ``$type``'s module only if it is already loaded, or its top-level package is
``griptape`` or ``griptape_nodes`` (this covers node library files, loaded lazily under
``griptape_nodes.node_libraries.*``). It builds only classes a codec covers, and building one
runs that codec's code on the data, so a class from any loaded module can be built. A value this
process cannot build or is not allowed to import, such as one whose class lives in a library
another process loads, decodes to an ``UndecodedValue`` that encodes back to exactly the data it
came from, so it passes through to a process that can build it.
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
import sys
import uuid
import weakref
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Protocol, Self, overload

import attrs
from griptape.mixins.serializable_mixin import SerializableMixin
from pydantic import BaseModel

from griptape_nodes.serialization.type_names import (
    IMPORTABLE_TOP_LEVEL_PACKAGES,
    ModuleUnavailableError,
    TypeNameError,
    resolve_type_name,
    stable_module_name,
    type_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TYPE_KEY = "$type"
VALUE_KEY = "$value"

type Value = Any
"""Any parameter value. Payload fields annotated with it cross the wire as tagged plain data."""

type DisplayValue = Any
"""A parameter value shown to a person, as in the editor. Crosses the wire like ``Value``, except
that a value with no plain-data form is sent as its text instead of failing."""

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


def encode_value(value: Any) -> JsonValue:
    """Return ``value`` as plain data that ``decode_value`` turns back into an equal value.

    Raises:
        ValueEncodeError: ``value``, or something inside it, has no plain-data form.
    """
    return _encode(value, set())


def encode_for_display(value: Any) -> JsonValue:
    """Return ``value`` encoded, or as its text if it has no plain-data form, for showing to a person."""
    try:
        return encode_value(value)
    except ValueEncodeError:
        return str(value)


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


def has_plain_data_form(cls: type) -> bool:
    """Whether instances of ``cls`` encode to plain data that decodes back to ``cls``."""
    return cls in _PLAIN_TYPES or cls in _BUILTIN_DECODERS or _codec_for(cls) is not None


class SavesState(Protocol):
    """What ``register_value_codec`` needs from a class it decorates."""

    def to_state(self) -> Any: ...

    @classmethod
    def from_state(cls, state: Any) -> Self: ...


@overload
def register_value_codec[T: SavesState](cls: type[T], /) -> type[T]: ...


@overload
def register_value_codec[T](
    cls: type[T], /, *, to_state: Callable[[T], Any], from_state: Callable[[Any], T]
) -> type[T]: ...


def register_value_codec(
    cls: type,
    /,
    *,
    to_state: Callable[[Any], Any] | None = None,
    from_state: Callable[[Any], Any] | None = None,
) -> type:
    """Make values of ``cls`` save and reopen.

    As a class decorator, for a class you own: the class's own ``to_state()`` and ``from_state()``
    classmethod are used, for it and its subclasses. With ``to_state`` and ``from_state``, for a
    class you cannot edit: they cover exactly ``cls``, which must have no other way to save.
    Register those from your library's ``before_library_nodes_loaded``, so every process that loads
    the library has them.

    Registering again from the same library, as when it reloads, replaces the codec. A class
    another library registered keeps that library's codec, with a warning.

    Raises:
        ValueError: ``cls`` belongs to Griptape, already saves another way, or, as a decorator,
            lacks ``to_state()`` or a ``from_state()`` classmethod.
        TypeError: only one of ``to_state`` and ``from_state`` was given.
    """
    owner = _owner_of(sys._getframe(1).f_globals.get("__name__", ""))
    registration = _registration_for(cls, to_state, from_state, owner)
    existing = _registered.get(cls)
    if existing is not None and existing.owner != owner:
        logger.warning(
            "Attempted to register how '%s' values save from '%s'. Kept the one '%s' registered, so "
            "values saved by '%s' reopen only where '%s' is installed.",
            cls.__qualname__,
            owner,
            existing.owner,
            owner,
            existing.owner,
        )
        return cls
    _registered[cls] = registration
    _codec_cache.clear()
    return cls


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
    codec = _codec_for(cls)
    if codec is not None:
        return _tagged(cls, _encode(_codec_state(codec, value), active))
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
    except ModuleUnavailableError as error:
        # Expected wherever a library's classes live in another process, so no warning.
        return UndecodedValue(data, str(error))
    except TypeNameError as error:
        return _undecodable(data, str(error))
    if VALUE_KEY not in data:
        state = decode_value({key: item for key, item in data.items() if key != TYPE_KEY})
    elif cls is dict and isinstance(data[VALUE_KEY], dict):
        # A wrapped dict: its own "$type" key is data, not a tag.
        state = {key: decode_value(item) for key, item in data[VALUE_KEY].items()}
    else:
        state = decode_value(data[VALUE_KEY])
    return _build(cls, state, data)


def _build(cls: type, state: Any, data: dict[str, Any]) -> Any:
    """Build ``cls`` from its decoded ``state``, in the order encoding chose its form."""
    builtin_decoder = _BUILTIN_DECODERS.get(cls)
    if builtin_decoder is not None:
        return _decode_builtin(builtin_decoder, cls, state, data)
    codec = _codec_for(cls)
    if codec is None:
        return UndecodedValue(data, f"'{data[TYPE_KEY]}' has no plain-data form in this process")
    return _decode_with_codec(codec, cls, state, data)


def _decode_with_codec(codec: _Codec, cls: type, state: Any, data: dict[str, Any]) -> Any:
    try:
        return codec.from_state(cls, state)
    except Exception as error:
        # Codecs run library code, which can raise anything.
        return _undecodable(data, f"a saved '{cls.__qualname__}' value could not be rebuilt: {error}")


def _decode_builtin(decoder: Any, cls: type, state: Any, data: dict[str, Any]) -> Any:
    if _holds_undecoded_key(cls, state):
        # A set member or dict key this process cannot build is a dict, which cannot be hashed.
        # Keep the whole value as data, quietly, as for any value whose class is elsewhere.
        return UndecodedValue(data, f"a saved '{data[TYPE_KEY]}' value holds values this process cannot build")
    try:
        return decoder(state)
    except (TypeError, ValueError, binascii.Error) as error:
        return _undecodable(data, f"a saved '{data[TYPE_KEY]}' value is malformed: {error}")


def _holds_undecoded_key(cls: type, state: Any) -> bool:
    """True if ``state`` is set members or dict pairs with an ``UndecodedValue`` where a key goes."""
    if not isinstance(state, list):
        return False
    if cls in (set, frozenset):
        return any(type(item) is UndecodedValue for item in state)
    if cls is dict:
        return any(isinstance(pair, list) and pair and type(pair[0]) is UndecodedValue for pair in state)
    return False


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


def _codec_state(codec: _Codec, value: Any) -> Any:
    try:
        return codec.to_state(value)
    except ValueEncodeError:
        raise
    except Exception as error:
        # Codecs run library code, which can raise anything.
        msg = f"A '{type(value).__qualname__}' value failed to produce its plain-data form: {error}"
        raise ValueEncodeError(msg) from error


def _codec_for(cls: type) -> _Codec | None:
    """A registered codec for ``cls`` or a decorated base class, else the first built-in rule that matches."""
    if cls in _codec_cache:
        return _codec_cache[cls]
    found = _registered_codec_for(cls)
    if found is None:
        found = _builtin_codec_for(cls)
    _codec_cache[cls] = found
    return found


def _registered_codec_for(cls: type) -> _Codec | None:
    for base in cls.__mro__:
        registration = _registered.get(base)
        if registration is not None and (base is cls or registration.covers_subclasses):
            return registration.codec
    return None


def _builtin_codec_for(cls: type) -> _Codec | None:
    return next((codec for matches, codec in _BUILTIN_CODECS if matches(cls)), None)


def _registration_for(
    cls: type,
    to_state: Callable[[Any], Any] | None,
    from_state: Callable[[Any], Any] | None,
    owner: str,
) -> _Registration:
    if cls in _PLAIN_DATA_TYPES or issubclass(cls, Path) or _is_griptape_class(cls):
        msg = f"Attempted to register how '{cls.__qualname__}' values save. Failed because Griptape already defines it."
        raise ValueError(msg)
    if to_state is None and from_state is None:
        if not (callable(getattr(cls, "to_state", None)) and _is_classmethod(cls, "from_state")):
            msg = (
                f"Attempted to register how '{cls.__qualname__}' values save. Failed because it needs a "
                "to_state() method and a from_state() classmethod."
            )
            raise ValueError(msg)
        return _Registration(_Codec(_call_to_state, _call_from_state), owner, covers_subclasses=True)
    if to_state is None or from_state is None:
        msg = f"Attempted to register how '{cls.__qualname__}' values save. Failed because it needs both to_state and from_state."
        raise TypeError(msg)
    already_saves = _registered_codec_for(cls) is not None or _builtin_codec_for(cls) is not None
    if cls not in _registered and already_saves:
        msg = (
            f"Attempted to register how '{cls.__qualname__}' values save. Failed because it already saves another way."
        )
        raise ValueError(msg)
    return _Registration(_Codec(to_state, _ignore_class(from_state)), owner, covers_subclasses=False)


def _owner_of(module_name: str) -> str:
    """The library a module belongs to, or the module itself outside a library."""
    stable = stable_module_name(module_name)
    if stable is not None and stable.startswith(_LIBRARY_NAMESPACE_PREFIX):
        return ".".join(stable.split(".")[:3])
    return module_name


def _is_griptape_class(cls: type) -> bool:
    return cls.__module__.partition(".")[0] in IMPORTABLE_TOP_LEVEL_PACKAGES and not cls.__module__.startswith(
        _LIBRARY_NAMESPACE_PREFIX
    )


def _is_classmethod(cls: type, name: str) -> bool:
    return isinstance(inspect.getattr_static(cls, name, None), classmethod)


def _call_to_state(value: Any) -> Any:
    return value.to_state()


def _call_from_state(cls: type, state: Any) -> Any:
    return cls.from_state(state)


def _ignore_class(from_state: Callable[[Any], Any]) -> Callable[[type, Any], Any]:
    return lambda _cls, state: from_state(state)


def _fields_state(value: Any) -> dict[str, Any]:
    if attrs.has(type(value)):
        return {field.alias: getattr(value, field.name) for field in attrs.fields(type(value)) if field.init}
    return {field.name: getattr(value, field.name) for field in dataclasses.fields(value) if field.init}


def _timedelta_state(value: datetime.timedelta) -> list[int]:
    return [value.days, value.seconds, value.microseconds]


def _timedelta_from_state(cls: type, state: list[int]) -> Any:
    days, seconds, microseconds = state
    return cls(days=days, seconds=seconds, microseconds=microseconds)


def _build_from_text(cls: type, state: str) -> Any:
    return cls(state)


def _build_from_fields(cls: type, state: dict[str, Any]) -> Any:
    return cls(**state)


@dataclasses.dataclass(frozen=True)
class _Codec:
    to_state: Callable[[Any], Any]
    from_state: Callable[[type, Any], Any]


@dataclasses.dataclass(frozen=True)
class _Registration:
    codec: _Codec
    owner: str
    covers_subclasses: bool


_LIBRARY_NAMESPACE_PREFIX = "griptape_nodes.node_libraries."

_PLAIN_TYPES: frozenset[type] = frozenset({type(None), bool, int, str, list, dict})

_BUILTIN_DECODERS: dict[type, Any] = {
    tuple: tuple,
    set: set,
    frozenset: frozenset,
    bytes: _decode_bytes,
    bytearray: _decode_bytearray,
    dict: _decode_dict,
    float: float,
    Path: Path,
}

# Encoded as themselves or by _encode directly, before any codec is looked up.
_PLAIN_DATA_TYPES = frozenset(
    {type(None), bool, int, float, str, bytes, bytearray, list, dict, tuple, set, frozenset, UndecodedValue}
)

# The engine's codecs, matched by rule and tried in order. Private: a rule can cover classes that
# belong to anyone, so libraries register by class instead.
_BUILTIN_CODECS: list[tuple[Callable[[type], bool], _Codec]] = [
    (lambda cls: issubclass(cls, enum.Enum), _Codec(lambda value: value.value, _build_from_text)),
    (lambda cls: issubclass(cls, PurePath), _Codec(str, _build_from_text)),
    (
        lambda cls: issubclass(cls, (datetime.date, datetime.time)),
        _Codec(lambda value: value.isoformat(), lambda cls, state: cls.fromisoformat(state)),
    ),
    (lambda cls: issubclass(cls, datetime.timedelta), _Codec(_timedelta_state, _timedelta_from_state)),
    (lambda cls: issubclass(cls, (uuid.UUID, decimal.Decimal)), _Codec(str, _build_from_text)),
    (
        lambda cls: issubclass(cls, tuple) and hasattr(cls, "_fields"),
        _Codec(lambda value: value._asdict(), _build_from_fields),
    ),
    (
        lambda cls: issubclass(cls, BaseModel),
        _Codec(lambda value: value.model_dump(mode="json"), lambda cls, state: cls.model_validate(state)),
    ),
    (
        lambda cls: issubclass(cls, SerializableMixin),
        _Codec(lambda value: value.to_dict(), lambda cls, state: cls.from_dict(state)),
    ),
    (lambda cls: dataclasses.is_dataclass(cls) or attrs.has(cls), _Codec(_fields_state, _build_from_fields)),
]

# Weak, so classes from a reloaded library file are not kept alive.
_registered: weakref.WeakKeyDictionary[type, _Registration] = weakref.WeakKeyDictionary()
_codec_cache: weakref.WeakKeyDictionary[type, _Codec | None] = weakref.WeakKeyDictionary()
