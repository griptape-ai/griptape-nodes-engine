"""Format specifier classes for macro variable transformations."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from griptape_nodes.common.macro_parser.exceptions import MacroResolutionError, MacroResolutionFailureReason


@dataclass
class FormatSpec(ABC):
    """Base class for format specifiers."""

    @abstractmethod
    def apply(self, value: str | int) -> str | int:
        """Apply this format spec to a value during resolution.

        Args:
            value: Value to transform

        Returns:
            Transformed value

        Raises:
            MacroResolutionError: If format cannot be applied to value type

        Examples:
            >>> # NumericPaddingFormat(width=3).apply(5)
            "005"
            >>> # LowerCaseFormat().apply("MyWorkflow")
            "myworkflow"
        """

    @abstractmethod
    def reverse(self, value: str) -> str | int:
        """Reverse this format spec during matching (best effort).

        Args:
            value: Formatted string value from a path

        Returns:
            Original value before format was applied

        Raises:
            MacroResolutionError: If value cannot be reversed

        Examples:
            >>> # NumericPaddingFormat(width=3).reverse("005")
            5
            >>> # SeparatorFormat(separator="_").reverse("workflow_")
            "workflow"
        """


@dataclass
class SeparatorFormat(FormatSpec):
    """Separator appended to variable value like :_, :/, :foo.

    Must be first format spec in list (if present).
    Syntax: {var:_} or {var:'lower'} (quotes to disambiguate from transformations)
    """

    separator: str  # e.g., "_", "/", "foo"

    def apply(self, value: str | int) -> str:
        """Append separator to value."""
        return str(value) + self.separator

    def reverse(self, value: str) -> str:
        """Remove separator from end of value."""
        if value.endswith(self.separator):
            return value[: -len(self.separator)]
        return value


@dataclass
class LeadingSeparatorFormat(FormatSpec):
    """Prefix that renders iff the variable emits — mirror of ``SeparatorFormat``.

    Emitted by ``{var?:^prefix}`` shorthand. The ``^`` at the start of the
    format-spec text is the discriminator; the remaining characters are the
    literal prefix payload. Applied by prepending to the (already
    format-spec-transformed) rendered value.

    Must be the LAST format spec on a variable, and there may be only one per
    variable. Both invariants are validated at parse time
    (``LEADING_SEPARATOR_NOT_LAST`` / ``MULTIPLE_LEADING_SEPARATORS``) so any
    inner transforms (``:upper``, ``:03``, etc.) run first and the prefix
    text is never mangled by them.

    Omit case is handled by the resolver's normal optional-variable path: an
    unbound optional variable skips its whole segment (including this spec),
    so an unbound ``{shot?:^_v}`` emits nothing, not ``_v``.
    """

    prefix: str  # e.g., "_v"; the ``^`` discriminator is stripped at parse time

    def apply(self, value: str | int) -> str:
        """Prepend ``prefix`` to the rendered value."""
        return self.prefix + str(value)

    def reverse(self, value: str) -> str:
        """Strip ``prefix`` from the front of the value if present.

        Idempotent when the prefix is absent, matching
        ``SeparatorFormat.reverse``'s "no-op if the trailing separator is
        missing" behavior. Consistent with the optional-variable contract:
        if the segment was omitted at resolve time, its ``reverse`` never
        runs; if the segment fired, the prefix will be present.
        """
        if value.startswith(self.prefix):
            return value[len(self.prefix) :]
        return value


@dataclass
class NumericPaddingFormat(FormatSpec):
    """Numeric padding format like :03, :04."""

    width: int  # e.g., 3 for :03

    def apply(self, value: str | int) -> str:
        """Apply numeric padding: 5 → "005"."""
        if not isinstance(value, int):
            if not str(value).isdigit():
                msg = (
                    f"Numeric padding format :{self.width:0{self.width}d} "
                    f"cannot be applied to non-numeric value: {value}"
                )
                raise MacroResolutionError(
                    msg,
                    failure_reason=MacroResolutionFailureReason.NUMERIC_PADDING_ON_NON_NUMERIC,
                )
            value = int(value)
        return f"{value:0{self.width}d}"

    def reverse(self, value: str) -> int:
        """Reverse numeric padding: "005" → 5."""
        try:
            return int(value)
        except ValueError as e:
            msg = f"Cannot parse '{value}' as integer"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.INVALID_INTEGER_PARSE,
            ) from e


@dataclass
class SequenceFormat(FormatSpec):
    """Sequence-slot marker emitted by `###`-style syntax (or wider runs like `####`).

    The presence of this format spec on a `ParsedVariable` means the slot is
    **system-allocated**: the CREATE_NEW write path is allowed to auto-fill it
    with a sequence number, and the `ScanSequencesRequest` handler recognizes
    it as the variable to enumerate over. Macro authors who instead write
    `{shot:03}` (numeric padding only, no `#` shorthand) are signalling user
    intent to bind that variable themselves; the legacy
    `NumericPaddingFormat`-on-lone-unresolved heuristic still treats those as
    system-allocated for backward compatibility, but new macros should use
    `###` to be unambiguous.

    ``min_width`` is a minimum render width: values smaller than
    ``10 ** min_width`` render zero-padded to ``min_width`` digits; values at
    or above the threshold render at their natural width (``_v999`` →
    ``_v1000``). Matches the universal `###` convention (ffmpeg ``%03d``,
    Houdini ``$F4``, Nuke ``####``, Python ``f"{n:03}"``). The zero-padded
    rendering behavior itself is identical to ``NumericPaddingFormat.apply`` —
    both use Python format specs, which never truncate — so the classes'
    ``apply`` / ``reverse`` bodies overlap by design.

    The real distinction between the two classes is **marker + glob
    semantics**, not rendering:

    - **Marker**: only ``SequenceFormat`` signals "system-allocated slot" at
      the type level. ``NumericPaddingFormat`` only tells the OSManager it's
      system-allocated via the legacy lone-unresolved heuristic, which is
      staged for retirement in #4991.
    - **Glob**: an unresolved ``SequenceFormat`` slot globs with a permissive
      ``*`` (any width — a value that overflows past ``min_width`` should
      still be a match). An unresolved ``NumericPaddingFormat`` slot globs
      with a fixed-count ``?`` — historical exact-width semantics, kept for
      the legacy heuristic path.

    A future cleanup (tracked separately) may collapse the shared
    ``apply`` / ``reverse`` code onto a common base to eliminate drift risk.
    """

    min_width: int  # e.g., 3 for ###

    def apply(self, value: str | int) -> str:
        """Render an integer with the slot's minimum width.

        Always int-normalizes the input before zero-padding, so two spellings
        of the same number produce the same output:

        - ``apply(5)`` with min_width=3 → ``"005"``
        - ``apply("5")`` with min_width=3 → ``"005"``
        - ``apply("0005")`` with min_width=3 → ``"005"`` (NOT ``"0005"``)
        - ``apply(1000)`` with min_width=3 → ``"1000"`` (value overflows the minimum width)

        Body is intentionally identical to ``NumericPaddingFormat.apply`` —
        both use Python's format-spec zero-padding, which never truncates.
        See the class docstring for why the two remain separate classes
        despite the overlap.
        """
        if not isinstance(value, int):
            if not str(value).isdigit():
                msg = f"Sequence format with min_width={self.min_width} cannot be applied to non-numeric value: {value}"
                raise MacroResolutionError(
                    msg,
                    failure_reason=MacroResolutionFailureReason.NUMERIC_PADDING_ON_NON_NUMERIC,
                )
            value = int(value)
        return f"{value:0{self.min_width}d}"

    def reverse(self, value: str) -> int:
        """Reverse a rendered sequence value back to an integer: "005" → 5."""
        try:
            return int(value)
        except ValueError as e:
            msg = f"Cannot parse '{value}' as integer"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.INVALID_INTEGER_PARSE,
            ) from e

    def render_pattern(self) -> str:
        """Return the bare hash-pattern glyphs (``###``) for this spec, sized to ``min_width``.

        Consumed by ``GetPathForMacroRequest``'s ``RENDER_SEQUENCE_PATTERN``
        behavior when the caller wants to preview a macro whose sequence slot
        isn't bound yet. Matches the ffmpeg / Houdini / Nuke convention where
        ``###`` reads universally as "an integer goes here" — the returned
        string is meant for display (destination fields, path previews), not
        for filesystem I/O. It contains no braces or ``?`` marker, so the
        surrounding text renders as the eventual on-disk shape rather than as
        residual macro syntax.
        """
        return "#" * self.min_width


@dataclass
class LowerCaseFormat(FormatSpec):
    """Lowercase transformation :lower."""

    def apply(self, value: str | int) -> str:
        """Convert value to lowercase."""
        return str(value).lower()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse case - return as-is."""
        return value


@dataclass
class UpperCaseFormat(FormatSpec):
    """Uppercase transformation :upper."""

    def apply(self, value: str | int) -> str:
        """Convert value to uppercase."""
        return str(value).upper()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse case - return as-is."""
        return value


@dataclass
class SlugFormat(FormatSpec):
    """Slugification format :slug (spaces to hyphens, safe chars only)."""

    def apply(self, value: str | int) -> str:
        """Convert to slug: spaces→hyphens, lowercase, safe chars."""
        s = str(value).lower()
        s = re.sub(r"\s+", "-", s)  # Spaces to hyphens
        s = re.sub(r"[^a-z0-9\-_]", "", s)  # Keep only safe chars
        return s

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse slugification - return as-is."""
        return value


@dataclass
class TitleCaseFormat(FormatSpec):
    """Title case transformation :title."""

    def apply(self, value: str | int) -> str:
        """Convert value to title case: 'hello world' → 'Hello World'."""
        return str(value).title()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse title case - return as-is."""
        return value


@dataclass
class SnakeCaseFormat(FormatSpec):
    """Snake case transformation :snake."""

    def apply(self, value: str | int) -> str:
        """Convert value to snake_case: 'Hello World' → 'hello_world'."""
        s = str(value)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
        s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
        s = re.sub(r"[\s\-]+", "_", s)
        return s.lower()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse snake_case - return as-is."""
        return value


@dataclass
class PascalCaseFormat(FormatSpec):
    """PascalCase transformation :pascal."""

    def apply(self, value: str | int) -> str:
        """Convert value to PascalCase: 'hello world' → 'HelloWorld'."""
        s = str(value)
        words = re.split(r"[\s_\-]+", s)
        return "".join(word.capitalize() for word in words if word)

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse PascalCase - return as-is."""
        return value


@dataclass
class CamelCaseFormat(FormatSpec):
    """camelCase transformation :camel."""

    def apply(self, value: str | int) -> str:
        """Convert value to camelCase: 'hello world' → 'helloWorld'."""
        s = str(value)
        words = re.split(r"[\s_\-]+", s)
        if not words:
            return s
        return words[0].lower() + "".join(word.capitalize() for word in words[1:] if word)

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse camelCase - return as-is."""
        return value


@dataclass
class TrimFormat(FormatSpec):
    """Trim whitespace transformation :trim."""

    def apply(self, value: str | int) -> str:
        """Strip leading and trailing whitespace."""
        return str(value).strip()

    def reverse(self, value: str) -> str:
        """Cannot reverse trim - return as-is."""
        return value


@dataclass
class AbbrevFormat(FormatSpec):
    """Abbreviation transformation :abbrev."""

    def apply(self, value: str | int) -> str:
        """Take first letter of each word: 'Hello World' → 'HW'."""
        s = str(value)
        words = re.split(r"[\s_\-]+", s)
        return "".join(word[0] for word in words if word)

    def reverse(self, value: str) -> str:
        """Cannot reverse abbreviation - return as-is."""
        return value


@dataclass
class DotCaseFormat(FormatSpec):
    """Dot case transformation :dot."""

    def apply(self, value: str | int) -> str:
        """Convert value to dot.case: 'Hello World' → 'hello.world'."""
        s = str(value)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1.\2", s)
        s = re.sub(r"([a-z\d])([A-Z])", r"\1.\2", s)
        s = re.sub(r"[\s_\-]+", ".", s)
        return s.lower()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse dot.case - return as-is."""
        return value


@dataclass
class ScreamingSnakeCaseFormat(FormatSpec):
    """SCREAMING_SNAKE_CASE transformation :screaming_snake."""

    def apply(self, value: str | int) -> str:
        """Convert value to SCREAMING_SNAKE_CASE: 'hello world' → 'HELLO_WORLD'."""
        s = str(value)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
        s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
        s = re.sub(r"[\s\-]+", "_", s)
        return s.upper()

    def reverse(self, value: str) -> str:
        """Cannot reliably reverse SCREAMING_SNAKE_CASE - return as-is."""
        return value


@dataclass
class DateFormat(FormatSpec):
    """Date formatting like :%Y-%m-%d."""

    pattern: str  # e.g., "%Y-%m-%d"

    def apply(self, _value: str | int) -> str:
        """Apply date formatting."""
        # TODO(https://github.com/griptape-ai/griptape-nodes/issues/2717): Implement date formatting
        msg = "DateFormat not yet fully implemented"
        raise MacroResolutionError(
            msg,
            failure_reason=MacroResolutionFailureReason.DATE_FORMAT_NOT_IMPLEMENTED,
        )

    def reverse(self, value: str) -> str:
        """Attempt to parse date string."""
        # TODO(https://github.com/griptape-ai/griptape-nodes/issues/2717): Implement date parsing
        return value


# Module-level registry of known format transformations
FORMAT_REGISTRY: dict[str, FormatSpec] = {
    "lower": LowerCaseFormat(),
    "upper": UpperCaseFormat(),
    "slug": SlugFormat(),
    "title": TitleCaseFormat(),
    "snake": SnakeCaseFormat(),
    "pascal": PascalCaseFormat(),
    "camel": CamelCaseFormat(),
    "trim": TrimFormat(),
    "abbrev": AbbrevFormat(),
    "dot": DotCaseFormat(),
    "screaming_snake": ScreamingSnakeCaseFormat(),
}
