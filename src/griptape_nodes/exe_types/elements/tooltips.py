"""The tooltip a parameter offers when its author did not write one."""

from __future__ import annotations

# Maximum number of input types to show in tooltip before truncating
MAX_TOOLTIP_INPUT_TYPES = 3

# How a type name reads to somebody looking at the parameter, rather than at code.
TYPE_DESCRIPTIONS = {
    "str": "text/string",
    "bool": "boolean (true/false)",
    "int": "integer number",
    "float": "decimal number",
    "any": "any type of data",
    "list": "list/array",
    "dict": "dictionary/object",
    "parametercontroltype": "control flow",
}


def default_parameter_tooltip(
    name: str,
    type: str | None,  # noqa: A002
    input_types: list[str] | None,
    output_type: str | None,
) -> str:
    """Generate a default tooltip describing the parameter type and usage.

    Args:
        name: The parameter name
        type: The parameter type
        input_types: List of accepted input types
        output_type: The output type

    Returns:
        A descriptive tooltip string
    """
    # Determine the primary type to describe
    primary_type = type
    if not primary_type and input_types:
        primary_type = input_types[0]
    if not primary_type and output_type:
        primary_type = output_type
    if not primary_type:
        primary_type = "any"

    type_desc = TYPE_DESCRIPTIONS.get(primary_type.lower(), primary_type)

    # Build the tooltip
    tooltip_parts = [f"Enter {type_desc} for {name}"]

    # Add input type info if different from primary type
    if input_types and len(input_types) > 1:
        input_desc = ", ".join(TYPE_DESCRIPTIONS.get(t.lower(), t) for t in input_types[:MAX_TOOLTIP_INPUT_TYPES])
        if len(input_types) > MAX_TOOLTIP_INPUT_TYPES:
            input_desc += f" or {len(input_types) - MAX_TOOLTIP_INPUT_TYPES} other types"
        tooltip_parts.append(f"Accepts: {input_desc}")

    return ". ".join(tooltip_parts) + "."
