"""The type language parameters speak: modes, builtin type names, and type comparison."""

from __future__ import annotations

from enum import Enum, StrEnum, auto
from typing import ClassVar, Literal, NamedTuple, get_args

# Parameter render location for control parameters - controls where they appear in the node UI
# "top": render at the top (default), "bottom": render at the bottom, "in-order": render in-line with other parameters
ParameterRenderLocation = Literal["top", "bottom", "in-order"]
VALID_PARAMETER_RENDER_LOCATIONS: frozenset[str] = frozenset(get_args(ParameterRenderLocation))


# Types of Modes provided for Parameters
class ParameterMode(Enum):
    OUTPUT = auto()
    INPUT = auto()
    PROPERTY = auto()


class ParameterTypeBuiltin(StrEnum):
    STR = "str"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    ANY = "any"
    NONE = "none"
    CONTROL_TYPE = "parametercontroltype"
    ALL = "all"


class ParameterType:
    class KeyValueTypePair(NamedTuple):
        """A named tuple for storing a pair of types for key-value parameters.

        Fields:
            key_type: The type of the key
            value_type: The type of the value
        """

        key_type: str
        value_type: str

    _builtin_aliases: ClassVar[dict] = {
        "str": ParameterTypeBuiltin.STR,
        "string": ParameterTypeBuiltin.STR,
        "bool": ParameterTypeBuiltin.BOOL,
        "boolean": ParameterTypeBuiltin.BOOL,
        "int": ParameterTypeBuiltin.INT,
        "float": ParameterTypeBuiltin.FLOAT,
        "any": ParameterTypeBuiltin.ANY,
        "none": ParameterTypeBuiltin.NONE,
        "parametercontroltype": ParameterTypeBuiltin.CONTROL_TYPE,
        "all": ParameterTypeBuiltin.ALL,
    }

    @staticmethod
    def attempt_get_builtin(type_name: str) -> ParameterTypeBuiltin | None:
        ret_val = ParameterType._builtin_aliases.get(type_name.lower())
        return ret_val

    @staticmethod
    def _extract_base_type(type_str: str) -> str:
        """Extract the base type from a potentially generic type string.

        Examples:
            'list[any]' -> 'list'
            'dict[str, int]' -> 'dict'
            'str' -> 'str'
        """
        bracket_index = type_str.find("[")
        if bracket_index == -1:
            return type_str
        return type_str[:bracket_index]

    @staticmethod
    def are_types_compatible(source_type: str | None, target_type: str | None) -> bool:  # noqa: PLR0911
        if source_type is None or target_type is None:
            return False

        source_type_lower = source_type.lower()
        target_type_lower = target_type.lower()

        # If either are None, bail.
        if ParameterTypeBuiltin.NONE.value in (source_type_lower, target_type_lower):
            return False
        if target_type_lower == ParameterTypeBuiltin.ANY.value:
            # If the TARGET accepts Any, we're good. Not always true the other way 'round.
            return True

        # First try exact match
        if source_type_lower == target_type_lower:
            return True

        source_base = ParameterType._extract_base_type(source_type_lower)
        target_base = ParameterType._extract_base_type(target_type_lower)

        # If base types match
        if source_base == target_base:
            # Allow any generic to flow to base type (list[any] -> list, list[str] -> list)
            if target_type_lower == target_base:
                return True

            # Allow specific types to flow to [any] generic (list[str] -> list[any])
            if target_type_lower == f"{target_base}[{ParameterTypeBuiltin.ANY.value}]":
                return True

        return False

    @staticmethod
    def parse_kv_type_pair(type_str: str) -> KeyValueTypePair | None:  # noqa: C901
        """Parse a string that potentially defines a Key-Value Type Pair.

        Args:
            type_str: A string like "[str, int]" or "[dict[str, bool], list[float]]"

        Returns:
            A KeyValueTypePair object if valid KV pair format, or None if not a KV pair

        Raises:
            ValueError: If the string appears to be a KV pair but is malformed
        """
        # Remove any whitespace
        type_str = type_str.strip()

        # Check if it starts with '[' and ends with ']'
        if not (type_str.startswith("[") and type_str.endswith("]")):
            return None  # Not a KV pair, just a regular type

        # Remove the outer brackets
        inner_content = type_str[1:-1].strip()

        # Now we need to find the comma that separates key type from value type
        # This is tricky because we might have nested structures with commas

        # Keep track of nesting level with different brackets
        bracket_stack = []
        comma_positions = []

        for i, char in enumerate(inner_content):
            if char in "[{(":
                bracket_stack.append(char)
            elif char in "]})":
                if bracket_stack:  # Ensure stack isn't empty
                    bracket_stack.pop()
                else:
                    # Unmatched closing bracket
                    err_str = f"Unmatched closing bracket at position {i} in '{type_str}'."
                    raise ValueError(err_str)
            elif char == "," and not bracket_stack:
                # This is a top-level comma
                comma_positions.append(i)

        # Check for unclosed brackets
        if bracket_stack:
            err_str = f"Unclosed brackets in '{type_str}'."
            raise ValueError(err_str)

        # We should have exactly one top-level comma
        if len(comma_positions) != 1:
            err_str = (
                f"Missing comma separator in '{type_str}'."
                if len(comma_positions) == 0
                else f"Too many comma separators in '{type_str}'."
            )
            raise ValueError(err_str)

        # Split at the comma
        key_type = inner_content[: comma_positions[0]].strip()
        value_type = inner_content[comma_positions[0] + 1 :].strip()

        # Validate that both parts are not empty
        if not key_type:
            err_str = f"Empty key type in '{type_str}'."
            raise ValueError(err_str)
        if not value_type:
            err_str = f"Empty value type in '{type_str}'."
            raise ValueError(err_str)

        return ParameterType.KeyValueTypePair(key_type=key_type, value_type=value_type)


def canonical_type_name(type_name: str) -> str:
    """Return the builtin name a type alias resolves to, or the name as it was written."""
    builtin = ParameterType.attempt_get_builtin(type_name)
    if builtin is None:
        return type_name
    return builtin.value


def accepts_incoming_type(input_types: list[str], incoming_type: str | None) -> bool:
    """Whether something declaring ``input_types`` may receive a value of ``incoming_type``."""
    if incoming_type is None:
        return False

    if incoming_type.lower() == ParameterTypeBuiltin.ALL.value:
        return True

    if not input_types:
        # Customer feedback was to treat as a string by default.
        return ParameterType.are_types_compatible(source_type=incoming_type, target_type=ParameterTypeBuiltin.STR.value)

    for test_type in input_types:
        if ParameterType.are_types_compatible(source_type=incoming_type, target_type=test_type):
            return True
    return False


def modes_from_flags(*, allow_input: bool, allow_property: bool, allow_output: bool) -> set[ParameterMode]:
    """Return the modes the ``allow_*`` arguments ask for."""
    modes: set[ParameterMode] = set()
    if allow_input:
        modes.add(ParameterMode.INPUT)
    if allow_property:
        modes.add(ParameterMode.PROPERTY)
    if allow_output:
        modes.add(ParameterMode.OUTPUT)
    return modes


def disallowed_mode_flags(*, allow_input: bool, allow_property: bool, allow_output: bool) -> list[str]:
    """Name the ``allow_*`` arguments turned off, as a caller wrote them."""
    disallowed: list[str] = []
    if not allow_input:
        disallowed.append("allow_input=False")
    if not allow_property:
        disallowed.append("allow_property=False")
    if not allow_output:
        disallowed.append("allow_output=False")
    return disallowed


def modes_with(modes: set[ParameterMode], mode: ParameterMode, *, allowed: bool) -> set[ParameterMode]:
    """Return ``modes`` with ``mode`` added or removed, leaving the original alone."""
    updated = modes.copy()
    if allowed:
        updated.add(mode)
    else:
        updated.discard(mode)
    return updated
