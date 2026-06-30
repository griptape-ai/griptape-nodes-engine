"""Macro language parser for template-based path generation."""

from griptape_nodes.common.macro_parser.core import ParsedMacro
from griptape_nodes.common.macro_parser.exceptions import (
    MacroMatchFailure,
    MacroMatchFailureReason,
    MacroParseFailure,
    MacroParseFailureReason,
    MacroResolutionError,
    MacroResolutionFailure,
    MacroResolutionFailureReason,
    MacroSyntaxError,
)
from griptape_nodes.common.macro_parser.formats import (
    DateFormat,
    LowerCaseFormat,
    NumericPaddingFormat,
    SeparatorFormat,
    SequenceFormat,
    SlugFormat,
    UpperCaseFormat,
)
from griptape_nodes.common.macro_parser.segments import (
    MacroVariables,
    ParsedStaticValue,
    ParsedVariable,
    VariableInfo,
)

__all__ = [
    "DateFormat",
    "LowerCaseFormat",
    "MacroMatchFailure",
    "MacroMatchFailureReason",
    "MacroParseFailure",
    "MacroParseFailureReason",
    "MacroResolutionError",
    "MacroResolutionFailure",
    "MacroResolutionFailureReason",
    "MacroSyntaxError",
    "MacroVariables",
    "NumericPaddingFormat",
    "ParsedMacro",
    "ParsedStaticValue",
    "ParsedVariable",
    "SeparatorFormat",
    "SequenceFormat",
    "SlugFormat",
    "UpperCaseFormat",
    "VariableInfo",
]
