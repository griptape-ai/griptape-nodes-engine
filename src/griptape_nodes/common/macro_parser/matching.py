"""Matching logic for extracting variables from paths."""

from __future__ import annotations

from typing import NamedTuple

from griptape_nodes.common.macro_parser.exceptions import MacroParseFailureReason, MacroSyntaxError
from griptape_nodes.common.macro_parser.formats import FormatSpec, LeadingSeparatorFormat, SeparatorFormat
from griptape_nodes.common.macro_parser.segments import (
    ParsedSegment,
    ParsedStaticValue,
    ParsedVariable,
    VariableInfo,
)


class NextAnchor(NamedTuple):
    """Next-anchor text and its provenance.

    The provenance flag drives extraction direction: leading-separator
    anchors search rightward (rfind) because a base name may legitimately
    contain the anchor text as a substring; static anchors search leftward
    (find) because they're literal delimiters at grammar-defined positions.
    See ``extract_single_variable`` for the full argument.
    """

    text: str
    from_leading_separator: bool


def extract_unknown_variables(
    pattern_segments: list[ParsedSegment],
    path: str,
) -> dict[VariableInfo, str | int] | None:
    """Extract unknown variable values from path (greedy matching).

    Args:
        pattern_segments: Partially resolved segments to match against
        path: Path string to extract variables from

    Returns:
        Dict mapping VariableInfo to extracted values, or None if no match.
    """
    current_match: dict[VariableInfo, str | int] = {}
    current_pos = 0

    for i, segment in enumerate(pattern_segments):
        match segment:
            case ParsedStaticValue():
                # Verify static text matches at current position
                if not path[current_pos:].startswith(segment.text):
                    # Static text doesn't match at this position
                    return None
                current_pos += len(segment.text)
            case ParsedVariable():
                result = extract_single_variable(segment, pattern_segments[i + 1 :], path, current_pos)
                if result is None:
                    return None
                value, new_pos = result
                current_match[segment.info] = value
                current_pos = new_pos
            case _:
                msg = f"Unexpected segment type: {type(segment).__name__}"
                raise MacroSyntaxError(
                    msg,
                    failure_reason=MacroParseFailureReason.UNEXPECTED_SEGMENT_TYPE,
                )

    return current_match


def extract_single_variable(
    variable: ParsedVariable,
    remaining_segments: list[ParsedSegment],
    path: str,
    start_pos: int,
) -> tuple[str | int, int] | None:
    """Extract value for a single variable from path.

    Args:
        variable: The variable to extract
        remaining_segments: Segments after this variable
        path: Full path being matched
        start_pos: Position in path to start extraction

    Returns:
        Tuple of (extracted_value, new_position) or None if extraction fails.
    """
    # Two candidate boundaries for this variable's greedy extraction:
    #
    # 1. Next anchor — text emitted by a FOLLOWING segment: the next
    #    `ParsedStaticValue` text, or the prefix of a following
    #    `ParsedVariable`'s `LeadingSeparatorFormat`. Critical for
    #    `{name}{###?:^_v}.png` where the following optional's `_v`
    #    prefix marks where `{name}` ends (see #5025).
    # 2. Self-anchor — this variable's OWN trailing `SeparatorFormat`. If it
    #    emits `<value>_`, some `_` in the path bounds the value. Critical
    #    for adjacent-variable layouts like `{a?:_}{b?:_}file.png` where
    #    the next segment has no discoverable anchor of its own.
    #
    # When both are available, direction matters: if the immediate next
    # segment is a static text (or a variable whose known-text anchor is
    # right up against ours), take the LATEST self-anchor before that text
    # (tightest split). Otherwise — the next segment is another variable
    # with its own separator to fill — take the FIRST self-anchor (leaves
    # room for the following variable to consume its share).
    self_separator = _trailing_separator(variable)
    next_anchor = find_next_anchor(remaining_segments)
    next_is_static = bool(remaining_segments) and isinstance(remaining_segments[0], ParsedStaticValue)

    next_end_pos: int | None = None
    if next_anchor is not None:
        anchor_pos = _locate_next_anchor(next_anchor, remaining_segments, path, start_pos)
        if anchor_pos == -1:
            return None
        next_end_pos = anchor_pos

    self_end_pos: int | None = None
    if self_separator is not None:
        search_limit = next_end_pos if next_end_pos is not None else len(path)
        # Direction of the self-anchor search:
        # - If the NEXT segment is a static, we want the tightest split against
        #   that static: LATEST self-anchor before it. That's the
        #   `{workflow_name?:_}photo.jpg` case.
        # - Otherwise (next segment is a variable — adjacent-optional layout),
        #   pick the FIRST self-anchor. That leaves the maximum tail for the
        #   following variables to consume: `{a?:_}{b?:_}file.png` on
        #   `first_second_file.png` → a=first, b=second.
        if next_is_static:
            sep_pos = path.rfind(self_separator, start_pos, search_limit)
        else:
            sep_pos = path.find(self_separator, start_pos, search_limit)
        if sep_pos != -1:
            self_end_pos = sep_pos + len(self_separator)

    if self_end_pos is not None:
        end_pos = self_end_pos
    elif next_end_pos is not None:
        end_pos = next_end_pos
    elif self_separator is None and next_anchor is None:
        # No more anchors - consume to end
        end_pos = len(path)
    else:
        # An anchor was expected but not found - no match
        return None

    # Extract raw value
    raw_value = path[start_pos:end_pos]

    # Reverse format specs
    reversed_value = reverse_format_specs(raw_value, variable.format_specs)
    if reversed_value is None:
        # Can't reverse format specs - no match
        return None

    return (reversed_value, end_pos)


def find_next_anchor(segments: list[ParsedSegment]) -> NextAnchor | None:
    """Find the next fixed text that bounds a preceding variable's extraction.

    An anchor is either:
    - The text of the next `ParsedStaticValue` segment
      (``from_leading_separator=False``), or
    - The `prefix` of a `LeadingSeparatorFormat` on the next `ParsedVariable`
      (e.g. `_v` inside `{###?:^_v}`), which is fixed text emitted iff the
      variable emits (``from_leading_separator=True``).

    The former is authoritative — a static segment ALWAYS appears in the path.
    The latter is fixed text only when the following variable emits; if it
    doesn't emit, callers may over-shrink the extraction range. That is
    acceptable here because the reverse-match orchestrator
    (`find_matches_detailed`) runs each of 2**k emitted/omitted combinations
    and validates via forward round-trip — an over-shrunk range simply fails
    to round-trip and the next mask is tried.

    The provenance flag on the returned ``NextAnchor`` drives the direction
    of the search in ``extract_single_variable`` (leftmost for static,
    rightmost for leading-separator — see the docstring there).

    Whichever comes FIRST in `segments` wins — the caller wants the
    tightest boundary.

    Args:
        segments: List of segments to search

    Returns:
        ``NextAnchor(text, from_leading_separator)``, or ``None`` if no
        anchor is available.
    """
    for seg in segments:
        if isinstance(seg, ParsedStaticValue):
            return NextAnchor(text=seg.text, from_leading_separator=False)
        if isinstance(seg, ParsedVariable):
            leading_prefix = _leading_separator_prefix(seg)
            if leading_prefix is not None:
                return NextAnchor(text=leading_prefix, from_leading_separator=True)
    return None


def _locate_next_anchor(
    next_anchor: NextAnchor,
    remaining_segments: list[ParsedSegment],
    path: str,
    start_pos: int,
) -> int:
    """Locate ``next_anchor`` in ``path`` at or after ``start_pos``.

    Search direction depends on provenance:

    - Static-derived anchors (``/``, ``.png``, ...) are literal delimiters at
      grammar-defined positions. Their FIRST occurrence in the path is the
      boundary — greedy left-to-right parsing for ``{a}/{b}/{c}`` relies
      on this.
    - Leading-separator-derived anchors (e.g. ``_v`` from ``{###?:^_v}``)
      are hints about where a FOLLOWING (usually optional) variable begins.
      A base name may legitimately contain the anchor text as a substring
      — e.g. ``my_v1_report_v007.py`` has two ``_v``. The trailing
      occurrence is where the version marker actually lives, so search
      rightward. See cjkindel review on #4989.

    For the rfind case, the search is bounded at the position of the next
    STATIC anchor (if any exists) so it can't overshoot into text that
    belongs to a downstream segment — e.g. ``{base}{###?:^_v}_video.{ext}``
    on ``my_report_v007_video.mp4``: without the bound, rfind would land on
    ``_v`` inside ``_video``. Unbounded rfind is fine when no static
    segment follows (nothing after the optional is anchored).

    The bound uses the FIRST occurrence of the downstream static — a
    deliberately conservative choice. If the downstream static's text also
    happens to appear earlier in the path (inside the base name, say), the
    bound over-tightens and rfind either returns ``-1`` (no anchor in the
    narrowed window) or a *shifted* anchor position pointing at an earlier
    lookalike rather than the true marker. Each failure mode has its own
    recovery path in the enclosing search:

    - ``-1`` → ``extract_single_variable`` returns ``None`` for the current
      variable, so the whole extraction attempt for this emitted/omitted
      mask fails; ``find_matches_detailed`` then tries the next mask.
    - Shifted positive → the extraction runs to completion and produces a
      variable bag, but the bag fails ``find_matches_detailed``'s forward
      round-trip against the original path, and the next mask is tried.

    This function is deliberately not the correctness boundary; it just
    narrows the search space and defers to the outer 2**k round-trip
    search in ``find_matches_detailed`` for the final "is this reading
    right" check.

    Returns the position, or ``-1`` if the anchor isn't found in the
    permitted range.
    """
    if not next_anchor.from_leading_separator:
        return path.find(next_anchor.text, start_pos)

    downstream_static_text = _find_downstream_static_text(remaining_segments)
    if downstream_static_text is None:
        return path.rfind(next_anchor.text, start_pos)
    downstream_static_pos = path.find(downstream_static_text, start_pos)
    if downstream_static_pos == -1:
        return path.rfind(next_anchor.text, start_pos)
    return path.rfind(next_anchor.text, start_pos, downstream_static_pos)


def _find_downstream_static_text(segments: list[ParsedSegment]) -> str | None:
    """Return the text of the next ``ParsedStaticValue`` segment, or None.

    Unlike ``find_next_anchor`` this ignores leading-separator anchors and
    walks past variables to the first genuinely-static segment — used only
    to bound the rightward rfind for a leading-separator anchor so the
    rfind can't overshoot into text that belongs to a downstream segment.
    See ``extract_single_variable`` for the argument.
    """
    for seg in segments:
        if isinstance(seg, ParsedStaticValue):
            return seg.text
    return None


def _leading_separator_prefix(variable: ParsedVariable) -> str | None:
    """Return the `LeadingSeparatorFormat.prefix` on ``variable`` if present."""
    for spec in variable.format_specs:
        if isinstance(spec, LeadingSeparatorFormat):
            return spec.prefix
    return None


def _trailing_separator(variable: ParsedVariable) -> str | None:
    """Return the ``SeparatorFormat.separator`` on ``variable`` if present.

    ``SeparatorFormat`` is a trailing separator — it renders as
    ``<value><sep>``. On the reverse path it's a self-anchor: the first
    occurrence of the separator in the path bounds this variable's extraction.
    """
    for spec in variable.format_specs:
        if isinstance(spec, SeparatorFormat):
            return spec.separator
    return None


def reverse_format_specs(value: str, format_specs: list[FormatSpec]) -> str | int | None:
    """Apply format spec reversal in reverse order.

    Args:
        value: String value extracted from path
        format_specs: List of format specs to reverse

    Returns:
        Reversed value (might be int after NumericPaddingFormat.reverse), or None if reversal fails.
    """
    result: str | int = value
    # Apply in reverse order (last spec first)
    for spec in reversed(format_specs):
        # reverse() expects str but result might be int, so convert if needed
        str_result = str(result) if isinstance(result, int) else result
        reversed_result = spec.reverse(str_result)
        if reversed_result is None:
            # Can't reverse this format spec
            return None
        result = reversed_result
    # Return reversed value (might be int after NumericPaddingFormat.reverse)
    return result
