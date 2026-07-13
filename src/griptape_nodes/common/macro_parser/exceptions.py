"""Exceptions for macro language parsing and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from griptape_nodes.common.macro_parser.segments import MacroVariables


class MacroSyntaxError(Exception):
    """Raised when macro template has invalid syntax.

    Examples of syntax errors:
    - Unbalanced braces: "{inputs}/{file_name"
    - Invalid format specifier: "{index:xyz}"
    - Nested braces: "{outer_{inner}}"
    """

    def __init__(
        self,
        message: str,
        failure_reason: MacroParseFailureReason | None = None,
        error_position: int | None = None,
    ) -> None:
        """Initialize MacroSyntaxError with optional structured fields.

        Args:
            message: Human-readable error message
            failure_reason: Specific reason for the syntax error
            error_position: Character position where error occurred
        """
        super().__init__(message)
        self.failure_reason = failure_reason
        self.error_position = error_position


class MacroResolutionError(Exception):
    """Raised when macro cannot be resolved with provided variables.

    Examples of resolution errors:
    - Required variable missing from variables dict
    - Environment variable referenced but not found in environment
    - Format specifier cannot be applied to value type (e.g., :03 on string)
    """

    def __init__(
        self,
        message: str,
        failure_reason: MacroResolutionFailureReason | None = None,
        variable_name: str | None = None,
        missing_variables: set[str] | None = None,
    ) -> None:
        """Initialize MacroResolutionError with optional structured fields.

        Args:
            message: Human-readable error message
            failure_reason: Specific reason for the resolution error
            variable_name: Name of the problematic variable (if applicable)
            missing_variables: Set of missing variable names (for MISSING_REQUIRED_VARIABLES)
        """
        super().__init__(message)
        self.failure_reason = failure_reason
        self.variable_name = variable_name
        self.missing_variables = missing_variables


class MacroMatchFailureReason(StrEnum):
    """Reason why path matching failed."""

    STATIC_TEXT_MISMATCH = "STATIC_TEXT_MISMATCH"
    DELIMITER_NOT_FOUND = "DELIMITER_NOT_FOUND"
    FORMAT_REVERSAL_FAILED = "FORMAT_REVERSAL_FAILED"
    INVALID_MACRO_SYNTAX = "INVALID_MACRO_SYNTAX"


class MacroParseFailureReason(StrEnum):
    """Reason why macro parsing failed."""

    UNMATCHED_CLOSING_BRACE = "UNMATCHED_CLOSING_BRACE"
    UNCLOSED_BRACE = "UNCLOSED_BRACE"
    NESTED_BRACES = "NESTED_BRACES"
    EMPTY_VARIABLE = "EMPTY_VARIABLE"
    UNEXPECTED_SEGMENT_TYPE = "UNEXPECTED_SEGMENT_TYPE"
    MULTIPLE_SEQUENCE_SLOTS = "MULTIPLE_SEQUENCE_SLOTS"
    EMPTY_LEADING_SEPARATOR = "EMPTY_LEADING_SEPARATOR"
    MULTIPLE_LEADING_SEPARATORS = "MULTIPLE_LEADING_SEPARATORS"
    TOO_MANY_OPTIONAL_VARIABLES = "TOO_MANY_OPTIONAL_VARIABLES"


class MacroResolutionFailureReason(StrEnum):
    """Reason why macro resolution failed."""

    NUMERIC_PADDING_ON_NON_NUMERIC = "NUMERIC_PADDING_ON_NON_NUMERIC"
    INVALID_INTEGER_PARSE = "INVALID_INTEGER_PARSE"
    DATE_FORMAT_NOT_IMPLEMENTED = "DATE_FORMAT_NOT_IMPLEMENTED"
    MISSING_REQUIRED_VARIABLES = "MISSING_REQUIRED_VARIABLES"
    ENVIRONMENT_VARIABLE_NOT_FOUND = "ENVIRONMENT_VARIABLE_NOT_FOUND"
    UNEXPECTED_SEGMENT_TYPE = "UNEXPECTED_SEGMENT_TYPE"


@dataclass
class MacroMatchFailure:
    """Details about why a macro match failed."""

    failure_reason: MacroMatchFailureReason
    expected_pattern: str
    known_variables_used: MacroVariables
    error_details: str


@dataclass
class MacroParseFailure:
    """Details about why macro parsing failed."""

    failure_reason: MacroParseFailureReason
    error_position: int | None
    error_details: str


@dataclass
class MacroResolutionFailure:
    """Details about why macro resolution failed."""

    failure_reason: MacroResolutionFailureReason
    variable_name: str | None
    missing_variables: set[str] | None
    error_details: str
