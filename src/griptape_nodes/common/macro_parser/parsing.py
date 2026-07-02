"""Parsing logic for macro templates."""

from __future__ import annotations

import re

from griptape_nodes.common.macro_parser.exceptions import MacroParseFailureReason, MacroSyntaxError
from griptape_nodes.common.macro_parser.formats import (
    FORMAT_REGISTRY,
    DateFormat,
    FormatSpec,
    LeadingSeparatorFormat,
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

# Canonical name for the variable emitted by `{###}`-style sequence slots.
# Matches the legacy `{_index:NN}` convention so downstream code (OSManager
# seed/walk, reverse-match against existing files) doesn't need a separate
# code path for the new syntax.
SEQUENCE_VARIABLE_NAME = "_index"

# Pattern for the sequence-slot shorthand `{###}` / `{##?}` (the content
# between the braces, after the leading-brace and trailing-brace are stripped
# by ``parse_segments``). One or more `#` characters, optionally followed by
# `?` for an optional slot. Anything else inside the braces — extra format
# specs, default values, mixed content — is NOT a sequence slot; it parses
# as a regular variable named with the `#` chars (which would then surface
# as a `MISSING_REQUIRED_VARIABLES` error downstream, making the user's
# typo visible). Wrapping the sigil in `{}` matches the rest of the macro
# grammar and avoids escaping conflicts with markdown's `#` header syntax.
_SEQUENCE_SHORTHAND_RE = re.compile(r"^(#+)(\?)?$")


def parse_segments(template: str) -> list[ParsedSegment]:
    """Parse template into alternating static/variable segments.

    Recognizes two kinds of variable syntax:

    1. ``{name}`` / ``{name:format}`` / ``{name?:format}`` — explicit
       variable references parsed by ``parse_variable``.
    2. ``{###}`` / ``{##?}`` (or other widths: ``{#}``, ``{####}``,
       ``{#####?}``, ...) — sequence-slot shorthand. The content between
       the braces is a run of ``N`` hash characters, optionally followed
       by ``?`` to mark the slot optional. Desugars to a ``ParsedVariable``
       with name ``SEQUENCE_VARIABLE_NAME``, ``is_required`` from the
       trailing ``?``, and a single ``SequenceFormat(min_width=N)`` spec.
       See issue #4902.

    A macro may contain at most one sequence-slot. If two are present
    the parser raises ``MacroSyntaxError`` — the writer likely meant to
    bind one as a user-supplied integer; OSManager won't know which to
    auto-allocate.

    Args:
        template: Template string to parse

    Returns:
        List of ParsedSegment (static and variable)

    Raises:
        MacroSyntaxError: If template syntax is invalid
    """
    segments: list[ParsedSegment] = []
    current_pos = 0

    while current_pos < len(template):
        # Find next opening brace
        brace_start = template.find("{", current_pos)

        if brace_start == -1:
            # No more variables, rest is static text
            static_text = template[current_pos:]
            if static_text:
                if "}" in static_text:
                    closing_pos = current_pos + static_text.index("}")
                    msg = f"Unmatched closing brace at position {closing_pos}"
                    raise MacroSyntaxError(
                        msg,
                        failure_reason=MacroParseFailureReason.UNMATCHED_CLOSING_BRACE,
                        error_position=closing_pos,
                    )
                segments.append(ParsedStaticValue(text=static_text))
            break

        # Add static text before the brace (if any)
        if brace_start > current_pos:
            static_text = template[current_pos:brace_start]
            if "}" in static_text:
                closing_pos = current_pos + static_text.index("}")
                msg = f"Unmatched closing brace at position {closing_pos}"
                raise MacroSyntaxError(
                    msg,
                    failure_reason=MacroParseFailureReason.UNMATCHED_CLOSING_BRACE,
                    error_position=closing_pos,
                )
            segments.append(ParsedStaticValue(text=static_text))

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

    # Post-parse validation: at most one sequence-slot shorthand per macro.
    # Two would leave OSManager with no way to pick the auto-allocated slot.
    # Counted here (rather than during parsing) because `{###}` goes through
    # `parse_variable`, which has no global view of the template.
    _reject_multiple_sequence_slots(template, segments)

    return segments


def _reject_multiple_sequence_slots(template: str, segments: list[ParsedSegment]) -> None:
    """Raise ``MULTIPLE_SEQUENCE_SLOTS`` when more than one sequence-slot shorthand exists.

    Identifies a sequence slot by the presence of a ``SequenceFormat`` in
    the variable's ``format_specs``. The shorthand currently emits a
    single ``SequenceFormat`` and nothing else, so this is a stable
    discriminator.

    ``error_position`` is best-effort: locates the second occurrence of
    ``{`` in ``template`` that follows the first sequence-slot's position.
    Used only for human-friendly error reporting; the failure mode is
    unambiguous regardless of position accuracy.
    """
    sequence_slot_count = sum(
        1
        for seg in segments
        if isinstance(seg, ParsedVariable) and any(isinstance(spec, SequenceFormat) for spec in seg.format_specs)
    )
    if sequence_slot_count <= 1:
        return
    # Find the second `{` in the template for the error_position. This
    # heuristic gets the right spot in the common case (two `{###}` blocks);
    # in pathological templates it just points somewhere reasonable.
    first_brace = template.find("{")
    second_brace = template.find("{", first_brace + 1) if first_brace != -1 else -1
    msg = "More than one sequence-slot shorthand (`{###}` / `{##?}`) in macro template; only one is allowed"
    raise MacroSyntaxError(
        msg,
        failure_reason=MacroParseFailureReason.MULTIPLE_SEQUENCE_SLOTS,
        error_position=second_brace if second_brace != -1 else first_brace,
    )


def parse_variable(variable_content: str) -> ParsedVariable:
    """Parse a variable from its content (text between braces).

    Args:
        variable_content: Content between braces (e.g., "workflow_name?:_:lower")

    Returns:
        ParsedVariable with name, format specs, and default value

    Raises:
        MacroSyntaxError: If variable syntax is invalid
    """
    # Split off default value (|) before anything else — the default text
    # is opaque to the rest of the parser and can contain colons.
    default_value = None
    if "|" in variable_content:
        parts = variable_content.split("|", 1)
        variable_content = parts[0]
        default_value = parts[1]

    # Split off format specs (:) before matching the sequence-slot shorthand
    # so shorthand can carry additional format specs (e.g. `{###?:^_v}`).
    # Prior to #5023 the shorthand regex matched the WHOLE variable content;
    # any trailing format spec would have silently broken shorthand
    # recognition and turned `###?` into a bare variable name.
    variable_part = variable_content
    format_parts: list[str] = []
    if ":" in variable_content:
        pieces = variable_content.split(":")
        variable_part = pieces[0]
        format_parts = pieces[1:]

    # Sequence-slot shorthand `{###}` / `{##?}`. The name piece (pre-colon)
    # is purely `#` chars (one or more), optionally followed by `?` for an
    # optional slot. Synthesizes a variable with the canonical sequence
    # name and a `SequenceFormat` carrying min_width; additional format
    # specs after `:` (if any) are parsed as usual and appended.
    sequence_match = _SEQUENCE_SHORTHAND_RE.match(variable_part)
    if sequence_match is not None:
        hash_run, optional_marker = sequence_match.groups()
        format_specs: list[FormatSpec] = [SequenceFormat(min_width=len(hash_run))]
        format_specs.extend(parse_format_spec(format_part) for format_part in format_parts)
        _normalize_leading_separator_position(format_specs)
        return ParsedVariable(
            info=VariableInfo(name=SEQUENCE_VARIABLE_NAME, is_required=optional_marker is None),
            format_specs=format_specs,
            default_value=default_value,
        )

    # Regular variable: name[?][:format[:format...]][|default]
    format_specs = []
    is_required = True
    if format_parts:
        # Parse format specifiers
        for format_part in format_parts:
            format_spec = parse_format_spec(format_part)
            format_specs.append(format_spec)

        # Check if last format spec ends with unquoted ?
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

    # Check for optional marker (?) after variable name
    if variable_part.endswith("?"):
        name = variable_part[:-1]
        is_required = False
    else:
        name = variable_part

    _normalize_leading_separator_position(format_specs)
    info = VariableInfo(name=name, is_required=is_required)
    return ParsedVariable(info=info, format_specs=format_specs, default_value=default_value)


def _normalize_leading_separator_position(format_specs: list[FormatSpec]) -> None:
    """Move any ``LeadingSeparatorFormat`` to the end of ``format_specs`` in-place.

    A leading separator is semantically a **prefix to the rendered value** —
    it should apply *after* every other format spec on the same variable,
    regardless of where the author wrote it in the template. Rather than
    forcing the author to remember which end of the spec list the prefix
    belongs on, the parser normalizes here: `{shot:^_v:upper}` and
    `{shot:upper:^_v}` parse to the same ordered list of specs, and
    therefore render identically.

    Rejects only the genuinely ambiguous case: more than one
    ``LeadingSeparatorFormat`` on the same variable
    (``MULTIPLE_LEADING_SEPARATORS``) — no sensible answer to "does the
    second prefix prepend before or after the first?"
    """
    leading_indices = [i for i, spec in enumerate(format_specs) if isinstance(spec, LeadingSeparatorFormat)]
    if not leading_indices:
        return
    if len(leading_indices) > 1:
        msg = "More than one leading separator (`:^prefix`) in a single variable; only one is allowed"
        raise MacroSyntaxError(
            msg,
            failure_reason=MacroParseFailureReason.MULTIPLE_LEADING_SEPARATORS,
            error_position=0,
        )
    idx = leading_indices[0]
    if idx == len(format_specs) - 1:
        # Already at the end — no work to do.
        return
    format_specs.append(format_specs.pop(idx))


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

    # Leading-separator marker: `:^prefix` prepends `prefix` to the variable's
    # rendered value. Mirror of the unquoted trailing separator (`:_`, `:foo`)
    # that today falls through to the SeparatorFormat branch below. Emitted
    # by issue #5023. Applies last regardless of where the author wrote it —
    # ``_normalize_leading_separator_position`` moves it to the tail of
    # ``format_specs`` after this function returns.
    if format_text.startswith("^"):
        prefix = format_text[1:]
        if not prefix:
            msg = "Empty leading separator (`:^`) — supply the prefix text after the caret"
            raise MacroSyntaxError(
                msg,
                failure_reason=MacroParseFailureReason.EMPTY_LEADING_SEPARATOR,
                error_position=0,
            )
        return LeadingSeparatorFormat(prefix=prefix)

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
