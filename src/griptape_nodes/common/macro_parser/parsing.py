"""Parsing logic for macro templates."""

from __future__ import annotations

import re

from griptape_nodes.common.macro_parser.exceptions import MacroParseFailureReason, MacroSyntaxError
from griptape_nodes.common.macro_parser.formats import (
    FORMAT_REGISTRY,
    DateFormat,
    FormatSpec,
    NumericPaddingFormat,
    SeparatorFormat,
    SequenceFormat,
)
from griptape_nodes.common.macro_parser.segments import (
    ParsedSegment,
    ParsedStaticValue,
    ParsedVariable,
    VariableInfo,
)

# Canonical name for the variable emitted by `###`-style sequence slots.
# Matches the legacy `{_index:NN}` convention so downstream code (OSManager
# seed/walk, reverse-match against existing files) doesn't need a separate
# code path for the new syntax.
SEQUENCE_VARIABLE_NAME = "_index"


def parse_segments(template: str) -> list[ParsedSegment]:
    """Parse template into alternating static/variable segments.

    Recognizes two kinds of variable syntax:

    1. ``{name}`` / ``{name:format}`` / ``{name?:format}`` — explicit
       variable references parsed by ``parse_variable``.
    2. ``###`` (or ``#``, ``####``, ``#####``...) — sequence-slot shorthand.
       A run of ``N`` hash characters in static text desugars to a
       ``ParsedVariable`` with name ``SEQUENCE_VARIABLE_NAME`` and a single
       ``SequenceFormat(min_width=N)`` spec. See issue #4902.

    A macro may contain at most one sequence-slot shorthand. If two `#`
    runs are present the parser raises ``MacroSyntaxError`` — the writer
    likely meant to bind one as a user-supplied integer; OSManager won't
    know which to auto-allocate.

    Args:
        template: Template string to parse

    Returns:
        List of ParsedSegment (static and variable)

    Raises:
        MacroSyntaxError: If template syntax is invalid
    """
    segments: list[ParsedSegment] = []
    current_pos = 0
    # Mutable single-element counter passed to _split_static_text_with_sequence_slots
    # so the "one sequence slot per macro" rule is enforced across all invocations.
    sequence_slot_count: list[int] = [0]

    while current_pos < len(template):
        # Find next opening brace
        brace_start = template.find("{", current_pos)

        if brace_start == -1:
            # No more variables, rest is static text
            _split_static_text_with_sequence_slots(template[current_pos:], current_pos, segments, sequence_slot_count)
            break

        # Add static text before the brace (if any), splitting around `#` runs.
        if brace_start > current_pos:
            _split_static_text_with_sequence_slots(
                template[current_pos:brace_start], current_pos, segments, sequence_slot_count
            )

        # Find matching closing brace
        brace_end = template.find("}", brace_start)
        if brace_end == -1:
            msg = f"Unclosed brace at position {brace_start}"
            raise MacroSyntaxError(
                msg,
                failure_reason=MacroParseFailureReason.UNCLOSED_BRACE,
                error_position=brace_start,
            )

        # Check for nested braces (opening brace before closing brace)
        next_open = template.find("{", brace_start + 1)
        if next_open != -1 and next_open < brace_end:
            msg = f"Nested braces are not allowed at position {next_open}"
            raise MacroSyntaxError(
                msg,
                failure_reason=MacroParseFailureReason.NESTED_BRACES,
                error_position=next_open,
            )

        # Extract and parse the variable content
        variable_content = template[brace_start + 1 : brace_end]
        if not variable_content:
            msg = f"Empty variable at position {brace_start}"
            raise MacroSyntaxError(
                msg,
                failure_reason=MacroParseFailureReason.EMPTY_VARIABLE,
                error_position=brace_start,
            )

        variable = parse_variable(variable_content)
        segments.append(variable)

        # Move past the closing brace
        current_pos = brace_end + 1

    return segments


def parse_variable(variable_content: str) -> ParsedVariable:
    """Parse a variable from its content (text between braces).

    Args:
        variable_content: Content between braces (e.g., "workflow_name?:_:lower")

    Returns:
        ParsedVariable with name, format specs, and default value

    Raises:
        MacroSyntaxError: If variable syntax is invalid
    """
    # Parse variable content: name[?][:format[:format...]][|default]

    # Check for default value (|)
    default_value = None
    if "|" in variable_content:
        parts = variable_content.split("|", 1)
        variable_content = parts[0]
        default_value = parts[1]

    # Check for format specifiers (:)
    format_specs: list[FormatSpec] = []
    is_required = True
    if ":" in variable_content:
        parts = variable_content.split(":")
        variable_part = parts[0]
        format_parts = parts[1:]

        # Parse format specifiers
        for format_part in format_parts:
            format_spec = parse_format_spec(format_part)
            format_specs.append(format_spec)

        # Check if last format spec ends with unquoted ?
        if format_parts:
            last_format_part = format_parts[-1]

            # Check if it's quoted (quoted formats preserve ? as literal)
            is_quoted = last_format_part.startswith("'") and last_format_part.endswith("'")

            if not is_quoted and last_format_part.endswith("?"):
                # Strip the ? and re-parse the format
                stripped_format = last_format_part[:-1]
                if stripped_format:
                    # Re-parse without the ?
                    format_specs[-1] = parse_format_spec(stripped_format)
                else:
                    # Format was just "?", remove it entirely
                    format_specs.pop()

                # Mark variable as optional
                is_required = False
    else:
        variable_part = variable_content

    # Check for optional marker (?) after variable name
    if variable_part.endswith("?"):
        name = variable_part[:-1]
        is_required = False
    else:
        name = variable_part

    info = VariableInfo(name=name, is_required=is_required)
    return ParsedVariable(info=info, format_specs=format_specs, default_value=default_value)


def parse_format_spec(format_text: str) -> FormatSpec:
    """Parse a single format specifier.

    Args:
        format_text: Format specifier text (e.g., "lower", "03", "_")

    Returns:
        Appropriate FormatSpec subclass instance

    Raises:
        MacroSyntaxError: If format specifier is invalid
    """
    # Remove quotes if present (for explicit separators like 'lower')
    if format_text.startswith("'") and format_text.endswith("'"):
        # Quoted text is always a separator, even if it matches other keywords
        return SeparatorFormat(separator=format_text[1:-1])

    # Check for date format (starts with %)
    if format_text.startswith("%"):
        # Date format pattern like %Y-%m-%d
        return DateFormat(pattern=format_text)

    # Check for numeric padding (e.g., "03", "04")
    if re.match(r"^\d+$", format_text):
        width = int(format_text)
        # Numeric padding like 03 means pad to 3 digits with zeros
        return NumericPaddingFormat(width=width)

    # Check for known transformations
    if format_text in FORMAT_REGISTRY:
        # Known transformation keyword (lower, upper, slug)
        return FORMAT_REGISTRY[format_text]

    # Otherwise, treat as separator (unquoted text that doesn't match any format)
    return SeparatorFormat(separator=format_text)


def _split_static_text_with_sequence_slots(
    static_text: str,
    base_pos: int,
    segments: list[ParsedSegment],
    sequence_slot_count: list[int],
) -> None:
    """Append ``static_text`` to ``segments``, splitting around any sequence-slot `#` runs.

    Each contiguous run of one or more `#` characters becomes a synthesized
    ``ParsedVariable`` (sequence slot); text on either side stays as
    ``ParsedStaticValue``. ``base_pos`` is the offset of ``static_text[0]``
    in the original template, used for error reporting.

    ``sequence_slot_count`` is a single-element list used as a mutable
    counter — passed by reference so this helper can enforce the
    "at most one sequence-slot per macro" rule across multiple invocations
    from ``parse_segments``.

    Raises:
        MacroSyntaxError: For unmatched ``}`` in static text, or when this
        invocation would push the sequence-slot count above 1.
    """
    if not static_text:
        return
    # Reject unmatched `}` in static text — same rule as the brace-finding pass.
    if "}" in static_text:
        closing_pos = base_pos + static_text.index("}")
        msg = f"Unmatched closing brace at position {closing_pos}"
        raise MacroSyntaxError(
            msg,
            failure_reason=MacroParseFailureReason.UNMATCHED_CLOSING_BRACE,
            error_position=closing_pos,
        )
    # Walk the text, emitting static spans between `#` runs and a
    # synthesized ParsedVariable for each run.
    idx = 0
    while idx < len(static_text):
        hash_match = re.search(r"#+", static_text[idx:])
        if hash_match is None:
            trailing = static_text[idx:]
            if trailing:
                segments.append(ParsedStaticValue(text=trailing))
            return
        run_start = idx + hash_match.start()
        run_end = idx + hash_match.end()
        if run_start > idx:
            segments.append(ParsedStaticValue(text=static_text[idx:run_start]))
        sequence_slot_count[0] += 1
        if sequence_slot_count[0] > 1:
            msg = (
                f"More than one sequence-slot shorthand (`#`-run) in macro template; "
                f"only one is allowed (position {base_pos + run_start})"
            )
            raise MacroSyntaxError(
                msg,
                failure_reason=MacroParseFailureReason.MULTIPLE_SEQUENCE_SLOTS,
                error_position=base_pos + run_start,
            )
        # Synthesize the sequence-slot variable. Name + required flag mirror
        # the legacy `{_index:NN}` convention so downstream code stays unified.
        segments.append(
            ParsedVariable(
                info=VariableInfo(name=SEQUENCE_VARIABLE_NAME, is_required=True),
                format_specs=[SequenceFormat(min_width=run_end - run_start)],
                default_value=None,
            )
        )
        idx = run_end
