"""Implicit conversion on connect: wires whose value is converted to the type the target takes.

A target parameter opts in with ``Parameter(..., implicit_conversion=True)``. Only then may a wire
whose type the target does not accept connect, and only when a rule below converts between the two.
Every other parameter keeps rejecting such a wire, exactly as before.

The rules, by source type and target type:

* ``int`` -> ``float``: exact.
* ``float`` -> ``int``: drops the fractional part (truncates toward zero). Marked lossy.
* ``int``, ``float``, ``bool`` -> ``str``: the value's text.
* ``dict``, ``list`` -> ``json``: the value itself, once it is checked to be valid JSON.
* ``dict``, ``list``, ``json`` -> ``str``: JSON text.
* ``json``, ``str`` -> ``dict``, ``list``: parsed from JSON text. Can fail when the run passes text
  that isn't JSON of that shape.
* ``str`` -> ``json``: parsed from JSON text. Can fail.
* ``str`` -> ``int``, ``float``: the number the text spells. Can fail.
* ``any`` -> anything: an output that doesn't know its type until it runs (a parsed value, a file
  read in auto mode). The value's own type picks the rule when it arrives: the same type passes
  through, a rule above converts it, and anything else is handed to the target unchanged for the
  target's own converters to accept or refuse. Always marked can-fail.

The value is converted when it passes along the wire, not when the wire is made. A value that can't
be converted raises ``ImplicitConversionError``; it never becomes ``None``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.elements.parameter_types import ParameterType

if TYPE_CHECKING:
    from collections.abc import Callable

_PREVIEW_LENGTH = 80
_ANY = "any"


class ImplicitConversionError(ValueError):
    """A value passed along a converting wire couldn't be converted to the target's type."""


@dataclass(frozen=True)
class ImplicitConversion:
    """How a converting wire turns a value of ``source_type`` into one of ``target_type``.

    ``lossy`` marks a conversion that can discard information (``float`` to ``int``). ``may_fail``
    marks one that depends on the value and can fail at run time (text to a number or to JSON).
    """

    source_type: str
    target_type: str
    lossy: bool
    may_fail: bool
    converter: Callable[[Any], Any]

    def apply(self, value: Any) -> Any:
        """Convert ``value``. ``None`` passes through: no value is not a failed conversion."""
        if value is None:
            return None
        return self.converter(value)


def find_implicit_conversion(source_type: str | None, target_types: list[str]) -> ImplicitConversion | None:
    """The first rule that converts ``source_type`` to one of ``target_types``, or None if none does."""
    if source_type is None:
        return None
    source_base = ParameterType._extract_base_type(source_type.lower())
    if source_base == _ANY:
        return _conversion_from_any(target_types)
    for target_type in target_types:
        target_base = ParameterType._extract_base_type(target_type.lower())
        rule = _RULES.get((source_base, target_base))
        if rule is None:
            continue
        return ImplicitConversion(
            source_type=source_base,
            target_type=target_type,
            lossy=rule.lossy,
            may_fail=rule.may_fail,
            converter=rule.converter,
        )
    return None


def _conversion_from_any(target_types: list[str]) -> ImplicitConversion | None:
    """A can-fail conversion from an untyped output to the first specific type the target takes."""
    for target_type in target_types:
        target_base = ParameterType._extract_base_type(target_type.lower())
        if target_base == _ANY:
            continue
        return ImplicitConversion(
            source_type=_ANY,
            target_type=target_type,
            lossy=False,
            may_fail=True,
            converter=_from_any_to(target_base),
        )
    return None


def _value_type_name(value: Any) -> str:
    """The parameter type name of a value: the builtin's name, or the class name for artifacts."""
    # bool before int: bool is a subclass of int.
    for builtin in (bool, int, float, str, dict, list):
        if isinstance(value, builtin):
            return builtin.__name__
    return type(value).__name__.lower()


def _from_any_to(target_base: str) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        value_type = _value_type_name(value)
        if value_type == target_base:
            return value
        rule = _RULES.get((value_type, target_base))
        if rule is not None:
            return rule.converter(value)
        return value

    return convert


def preview_value(value: Any) -> str:
    """A short, readable rendering of a value for an error message."""
    text = repr(value)
    if len(text) <= _PREVIEW_LENGTH:
        return text
    return text[: _PREVIEW_LENGTH - 3] + "..."


@dataclass(frozen=True)
class _Rule:
    converter: Callable[[Any], Any]
    lossy: bool = False
    may_fail: bool = False


def _int_to_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{preview_value(value)} isn't a whole number"
        raise ImplicitConversionError(msg)
    return float(value)


def _float_to_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{preview_value(value)} isn't a number"
        raise ImplicitConversionError(msg)
    if not math.isfinite(value):
        msg = f"{preview_value(value)} has no whole-number equivalent"
        raise ImplicitConversionError(msg)
    return math.trunc(value)


def _to_text(value: Any) -> str:
    return str(value)


def _to_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as err:
        msg = f"{preview_value(value)} can't be written as JSON text ({err})"
        raise ImplicitConversionError(msg) from err


def _to_json_value(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as err:
        msg = f"{preview_value(value)} isn't valid JSON ({err})"
        raise ImplicitConversionError(msg) from err
    return value


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as err:
        msg = f"the text {preview_value(value)} isn't valid JSON ({err.msg})"
        raise ImplicitConversionError(msg) from err


def _parse_json_dict(value: Any) -> dict:
    parsed = _parse_json(value)
    if not isinstance(parsed, dict):
        msg = f"{preview_value(value)} isn't a JSON object"
        raise ImplicitConversionError(msg)
    return parsed


def _parse_json_list(value: Any) -> list:
    parsed = _parse_json(value)
    if not isinstance(parsed, list):
        msg = f"{preview_value(value)} isn't a JSON list"
        raise ImplicitConversionError(msg)
    return parsed


def _text_to_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value).strip())
    except ValueError as err:
        msg = f"the text {preview_value(value)} isn't a whole number"
        raise ImplicitConversionError(msg) from err


def _text_to_float(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError as err:
        msg = f"the text {preview_value(value)} isn't a number"
        raise ImplicitConversionError(msg) from err


_RULES: dict[tuple[str, str], _Rule] = {
    ("int", "float"): _Rule(converter=_int_to_float),
    ("float", "int"): _Rule(converter=_float_to_int, lossy=True),
    ("int", "str"): _Rule(converter=_to_text),
    ("float", "str"): _Rule(converter=_to_text),
    ("bool", "str"): _Rule(converter=_to_text),
    ("dict", "json"): _Rule(converter=_to_json_value),
    ("list", "json"): _Rule(converter=_to_json_value),
    ("dict", "str"): _Rule(converter=_to_json_text),
    ("list", "str"): _Rule(converter=_to_json_text),
    ("json", "str"): _Rule(converter=_to_json_text),
    ("json", "dict"): _Rule(converter=_parse_json_dict, may_fail=True),
    ("json", "list"): _Rule(converter=_parse_json_list, may_fail=True),
    ("str", "dict"): _Rule(converter=_parse_json_dict, may_fail=True),
    ("str", "list"): _Rule(converter=_parse_json_list, may_fail=True),
    ("str", "json"): _Rule(converter=_parse_json, may_fail=True),
    ("str", "int"): _Rule(converter=_text_to_int, may_fail=True),
    ("str", "float"): _Rule(converter=_text_to_float, may_fail=True),
}
