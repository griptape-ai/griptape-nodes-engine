"""Tests for macro parser functionality."""

# ruff: noqa: PLR2004

from typing import Any

import pytest

from griptape_nodes.common.macro_parser import (
    AbbrevFormat,
    CamelCaseFormat,
    DateFormat,
    DotCaseFormat,
    LeadingSeparatorFormat,
    LowerCaseFormat,
    MacroMatchFailure,
    MacroMatchFailureReason,
    MacroParseFailure,
    MacroParseFailureReason,
    MacroResolutionError,
    MacroResolutionFailure,
    MacroResolutionFailureReason,
    MacroSyntaxError,
    NumericPaddingFormat,
    ParsedMacro,
    ParsedStaticValue,
    ParsedVariable,
    PascalCaseFormat,
    ScreamingSnakeCaseFormat,
    SeparatorFormat,
    SequenceFormat,
    SlugFormat,
    SnakeCaseFormat,
    TitleCaseFormat,
    TrimFormat,
    UpperCaseFormat,
    VariableInfo,
)
from griptape_nodes.common.macro_parser.parsing import SEQUENCE_VARIABLE_NAME, parse_segments, parse_variable


class TestFormatSpecs:
    """Test cases for format specifier classes."""

    def test_separator_format_apply(self) -> None:
        """Test SeparatorFormat.apply() appends separator."""
        fmt = SeparatorFormat(separator="_")
        assert fmt.apply("workflow") == "workflow_"
        assert fmt.apply(123) == "123_"

    def test_separator_format_reverse(self) -> None:
        """Test SeparatorFormat.reverse() removes separator."""
        fmt = SeparatorFormat(separator="_")
        assert fmt.reverse("workflow_") == "workflow"
        assert fmt.reverse("workflow") == "workflow"  # No separator to remove

    def test_leading_separator_format_apply(self) -> None:
        """LeadingSeparatorFormat.apply() prepends the prefix."""
        fmt = LeadingSeparatorFormat(prefix="_v")
        assert fmt.apply("001") == "_v001"
        assert fmt.apply(5) == "_v5"

    def test_leading_separator_format_reverse(self) -> None:
        """LeadingSeparatorFormat.reverse() strips the prefix; idempotent when absent."""
        fmt = LeadingSeparatorFormat(prefix="_v")
        assert fmt.reverse("_v001") == "001"
        assert fmt.reverse("001") == "001"  # No prefix to remove — mirrors SeparatorFormat.reverse

    def test_numeric_padding_format_apply_int(self) -> None:
        """Test NumericPaddingFormat.apply() with integer."""
        fmt = NumericPaddingFormat(width=3)
        assert fmt.apply(5) == "005"
        assert fmt.apply(42) == "042"
        assert fmt.apply(123) == "123"
        assert fmt.apply(1234) == "1234"  # Doesn't truncate

    def test_numeric_padding_format_apply_string_digits(self) -> None:
        """Test NumericPaddingFormat.apply() with numeric string."""
        fmt = NumericPaddingFormat(width=3)
        assert fmt.apply("5") == "005"
        assert fmt.apply("42") == "042"

    def test_numeric_padding_format_apply_non_numeric_fails(self) -> None:
        """Test NumericPaddingFormat.apply() fails on non-numeric string."""
        fmt = NumericPaddingFormat(width=3)
        with pytest.raises(MacroResolutionError, match="cannot be applied to non-numeric value"):
            fmt.apply("abc")

    def test_numeric_padding_format_reverse(self) -> None:
        """Test NumericPaddingFormat.reverse() converts to int."""
        fmt = NumericPaddingFormat(width=3)
        assert fmt.reverse("005") == 5
        assert fmt.reverse("042") == 42
        assert fmt.reverse("123") == 123

    def test_numeric_padding_format_reverse_invalid(self) -> None:
        """Test NumericPaddingFormat.reverse() fails on non-numeric."""
        fmt = NumericPaddingFormat(width=3)
        with pytest.raises(MacroResolutionError, match="Cannot parse"):
            fmt.reverse("abc")

    def test_lowercase_format_apply(self) -> None:
        """Test LowerCaseFormat.apply() converts to lowercase."""
        fmt = LowerCaseFormat()
        assert fmt.apply("MyWorkflow") == "myworkflow"
        assert fmt.apply("HELLO") == "hello"
        assert fmt.apply(123) == "123"

    def test_lowercase_format_reverse(self) -> None:
        """Test LowerCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = LowerCaseFormat()
        assert fmt.reverse("myworkflow") == "myworkflow"

    def test_uppercase_format_apply(self) -> None:
        """Test UpperCaseFormat.apply() converts to uppercase."""
        fmt = UpperCaseFormat()
        assert fmt.apply("MyWorkflow") == "MYWORKFLOW"
        assert fmt.apply("hello") == "HELLO"
        assert fmt.apply(123) == "123"

    def test_uppercase_format_reverse(self) -> None:
        """Test UpperCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = UpperCaseFormat()
        assert fmt.reverse("MYWORKFLOW") == "MYWORKFLOW"

    def test_slug_format_apply(self) -> None:
        """Test SlugFormat.apply() slugifies value."""
        fmt = SlugFormat()
        assert fmt.apply("My Workflow") == "my-workflow"
        assert fmt.apply("Hello World!") == "hello-world"
        assert fmt.apply("test_123") == "test_123"
        assert fmt.apply("MIXED Case") == "mixed-case"

    def test_slug_format_reverse(self) -> None:
        """Test SlugFormat.reverse() returns as-is (cannot reverse)."""
        fmt = SlugFormat()
        assert fmt.reverse("my-workflow") == "my-workflow"

    def test_title_case_format_apply(self) -> None:
        """Test TitleCaseFormat.apply() converts to title case."""
        fmt = TitleCaseFormat()
        assert fmt.apply("hello world") == "Hello World"
        assert fmt.apply("HELLO WORLD") == "Hello World"
        assert fmt.apply("hello") == "Hello"
        assert fmt.apply(123) == "123"

    def test_title_case_format_reverse(self) -> None:
        """Test TitleCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = TitleCaseFormat()
        assert fmt.reverse("Hello World") == "Hello World"

    def test_snake_case_format_apply(self) -> None:
        """Test SnakeCaseFormat.apply() converts to snake_case."""
        fmt = SnakeCaseFormat()
        assert fmt.apply("Hello World") == "hello_world"
        assert fmt.apply("helloWorld") == "hello_world"
        assert fmt.apply("HelloWorld") == "hello_world"
        assert fmt.apply("already_snake") == "already_snake"
        assert fmt.apply("Hello-World") == "hello_world"

    def test_snake_case_format_reverse(self) -> None:
        """Test SnakeCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = SnakeCaseFormat()
        assert fmt.reverse("hello_world") == "hello_world"

    def test_pascal_case_format_apply(self) -> None:
        """Test PascalCaseFormat.apply() converts to PascalCase."""
        fmt = PascalCaseFormat()
        assert fmt.apply("hello world") == "HelloWorld"
        assert fmt.apply("hello_world") == "HelloWorld"
        assert fmt.apply("hello-world") == "HelloWorld"
        assert fmt.apply("hello") == "Hello"

    def test_pascal_case_format_reverse(self) -> None:
        """Test PascalCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = PascalCaseFormat()
        assert fmt.reverse("HelloWorld") == "HelloWorld"

    def test_camel_case_format_apply(self) -> None:
        """Test CamelCaseFormat.apply() converts to camelCase."""
        fmt = CamelCaseFormat()
        assert fmt.apply("hello world") == "helloWorld"
        assert fmt.apply("hello_world") == "helloWorld"
        assert fmt.apply("hello-world") == "helloWorld"
        assert fmt.apply("hello") == "hello"

    def test_camel_case_format_reverse(self) -> None:
        """Test CamelCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = CamelCaseFormat()
        assert fmt.reverse("helloWorld") == "helloWorld"

    def test_screaming_snake_case_format_apply(self) -> None:
        """Test ScreamingSnakeCaseFormat.apply() converts to SCREAMING_SNAKE_CASE."""
        fmt = ScreamingSnakeCaseFormat()
        assert fmt.apply("hello world") == "HELLO_WORLD"
        assert fmt.apply("helloWorld") == "HELLO_WORLD"
        assert fmt.apply("hello-world") == "HELLO_WORLD"
        assert fmt.apply("already_snake") == "ALREADY_SNAKE"

    def test_screaming_snake_case_format_reverse(self) -> None:
        """Test ScreamingSnakeCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = ScreamingSnakeCaseFormat()
        assert fmt.reverse("HELLO_WORLD") == "HELLO_WORLD"

    def test_dot_case_format_apply(self) -> None:
        """Test DotCaseFormat.apply() converts to dot.case."""
        fmt = DotCaseFormat()
        assert fmt.apply("Hello World") == "hello.world"
        assert fmt.apply("helloWorld") == "hello.world"
        assert fmt.apply("hello_world") == "hello.world"
        assert fmt.apply("hello-world") == "hello.world"

    def test_dot_case_format_reverse(self) -> None:
        """Test DotCaseFormat.reverse() returns as-is (cannot reverse)."""
        fmt = DotCaseFormat()
        assert fmt.reverse("hello.world") == "hello.world"

    def test_abbrev_format_apply(self) -> None:
        """Test AbbrevFormat.apply() takes first letter of each word."""
        fmt = AbbrevFormat()
        assert fmt.apply("Hello World") == "HW"
        assert fmt.apply("hello world") == "hw"
        assert fmt.apply("one two three") == "ott"
        assert fmt.apply("hello_world") == "hw"
        assert fmt.apply("hello-world") == "hw"

    def test_abbrev_format_reverse(self) -> None:
        """Test AbbrevFormat.reverse() returns as-is (cannot reverse)."""
        fmt = AbbrevFormat()
        assert fmt.reverse("HW") == "HW"

    def test_trim_format_apply(self) -> None:
        """Test TrimFormat.apply() strips leading and trailing whitespace."""
        fmt = TrimFormat()
        assert fmt.apply("  hello  ") == "hello"
        assert fmt.apply("hello") == "hello"
        assert fmt.apply("\thello\n") == "hello"
        assert fmt.apply(123) == "123"

    def test_trim_format_reverse(self) -> None:
        """Test TrimFormat.reverse() returns as-is (cannot reverse)."""
        fmt = TrimFormat()
        assert fmt.reverse("hello") == "hello"

    def test_date_format_not_implemented(self) -> None:
        """Test DateFormat raises not implemented error."""
        fmt = DateFormat(pattern="%Y-%m-%d")
        with pytest.raises(MacroResolutionError, match="not yet fully implemented"):
            fmt.apply("2025-10-16")

    # --- SequenceFormat (`###` shorthand; issue #4902) ---

    def test_sequence_format_apply_int_below_min_width(self) -> None:
        """Values below 10**min_width zero-pad to min_width characters."""
        fmt = SequenceFormat(min_width=3)
        assert fmt.apply(1) == "001"
        assert fmt.apply(42) == "042"
        assert fmt.apply(999) == "999"

    def test_sequence_format_apply_int_overflow_renders_natural_width(self) -> None:
        """Values at or above 10**min_width render at natural width — no truncation.

        Pin the `###` convention (matches ffmpeg `%03d`, Python's `:03`, etc.):
        the width is a *minimum*. A sequence with thousands of items keeps
        going past `999` rather than collapsing to `000` or truncating.
        """
        fmt = SequenceFormat(min_width=3)
        assert fmt.apply(1000) == "1000"
        assert fmt.apply(123456) == "123456"

    def test_sequence_format_apply_widths_one_four_five(self) -> None:
        """`#`, `####`, `#####` produce widths 1, 4, 5."""
        assert SequenceFormat(min_width=1).apply(5) == "5"
        assert SequenceFormat(min_width=4).apply(42) == "0042"
        assert SequenceFormat(min_width=5).apply(42) == "00042"

    def test_sequence_format_apply_string_digits(self) -> None:
        """Numeric strings parse as int then zero-pad."""
        fmt = SequenceFormat(min_width=4)
        assert fmt.apply("5") == "0005"
        assert fmt.apply("1000") == "1000"

    def test_sequence_format_apply_string_with_leading_zeros_is_normalized(self) -> None:
        """`apply("0004")` with min_width=3 returns "004" — input is a quantity, not a glyph string.

        Without int-normalization the str-then-zfill path would preserve the
        caller's leading zeros and return "0004", which is inconsistent with
        `NumericPaddingFormat` (which int-normalizes) and surprising for two
        different string spellings of the same number. Pin the contract:
        leading zeros in input are stripped, then the value is re-padded to
        `min_width`.
        """
        fmt = SequenceFormat(min_width=3)
        assert fmt.apply("0004") == "004"
        assert fmt.apply("00000005") == "005"
        # Overflow case: leading zeros stripped first, then natural-width render.
        assert fmt.apply("01000") == "1000"

    def test_sequence_format_apply_non_numeric_raises(self) -> None:
        """Applying to a non-numeric string raises with the sequence-format identifier."""
        fmt = SequenceFormat(min_width=3)
        with pytest.raises(MacroResolutionError, match="cannot be applied to non-numeric value"):
            fmt.apply("abc")

    def test_sequence_format_reverse_returns_int(self) -> None:
        """Reverse parses the rendered string back to an int."""
        fmt = SequenceFormat(min_width=3)
        assert fmt.reverse("005") == 5
        assert fmt.reverse("042") == 42
        assert fmt.reverse("1000") == 1000

    def test_sequence_format_reverse_invalid_raises(self) -> None:
        """Reverse on a non-numeric string raises."""
        fmt = SequenceFormat(min_width=3)
        with pytest.raises(MacroResolutionError, match="Cannot parse"):
            fmt.reverse("abc")

    def test_sequence_format_render_pattern_emits_bare_hashes(self) -> None:
        """render_pattern emits the bare ``###`` glyphs at ``min_width`` — no braces, no ``?``."""
        assert SequenceFormat(min_width=1).render_pattern() == "#"
        assert SequenceFormat(min_width=3).render_pattern() == "###"
        assert SequenceFormat(min_width=5).render_pattern() == "#####"


class TestParsedMacro:
    """Test cases for ParsedMacro class."""

    def test_parsed_macro_initialization(self) -> None:
        """Test ParsedMacro can be initialized and parses template."""
        macro = ParsedMacro("inputs/{file_name}")

        assert macro.template == "inputs/{file_name}"
        assert len(macro.segments) == 2
        assert isinstance(macro.segments[0], ParsedStaticValue)
        assert macro.segments[0].text == "inputs/"
        assert isinstance(macro.segments[1], ParsedVariable)
        assert macro.segments[1].info.name == "file_name"

    def test_get_variables_extracts_variable_info(self) -> None:
        """Test get_variables() extracts VariableInfo from segments."""
        macro = ParsedMacro("{inputs}/{workflow_name?:_}{file_name}")

        variables = macro.get_variables()

        assert len(variables) == 3
        assert variables == {
            VariableInfo(name="inputs", is_required=True),
            VariableInfo(name="workflow_name", is_required=False),
            VariableInfo(name="file_name", is_required=True),
        }

    def test_get_variables_empty_for_no_variables(self) -> None:
        """Test get_variables() returns empty set when no variables."""
        macro = ParsedMacro("static/path/only")

        variables = macro.get_variables()

        assert len(variables) == 0
        assert variables == set()


class TestMacroParserParseVariable:
    """Test cases for parse_variable() function."""

    def test_parse_variable_simple_required(self) -> None:
        """Test parsing simple required variable."""
        variable = parse_variable("file_name")

        assert variable.info.name == "file_name"
        assert variable.info.is_required is True
        assert len(variable.format_specs) == 0
        assert variable.default_value is None

    def test_parse_variable_optional(self) -> None:
        """Test parsing optional variable."""
        variable = parse_variable("workflow_name?")

        assert variable.info.name == "workflow_name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 0

    def test_parse_variable_with_separator(self) -> None:
        """Test parsing variable with separator format."""
        variable = parse_variable("workflow_name?:_")

        assert variable.info.name == "workflow_name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "_"

    def test_parse_variable_with_multiple_formats(self) -> None:
        """Test parsing variable with multiple format specifiers."""
        variable = parse_variable("workflow_name?:_:lower")

        assert variable.info.name == "workflow_name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 2
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert isinstance(variable.format_specs[1], LowerCaseFormat)

    def test_parse_variable_with_numeric_padding(self) -> None:
        """Test parsing variable with numeric padding format."""
        variable = parse_variable("index:03")

        assert variable.info.name == "index"
        assert variable.info.is_required is True
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], NumericPaddingFormat)
        assert variable.format_specs[0].width == 3

    def test_parse_variable_with_default_value(self) -> None:
        """Test parsing variable with default value."""
        variable = parse_variable("name|default_value")

        assert variable.info.name == "name"
        assert variable.default_value == "default_value"

    def test_parse_variable_with_quoted_separator(self) -> None:
        """Test parsing variable with quoted separator (disambiguate from transformation)."""
        variable = parse_variable("name:'lower'")

        assert variable.info.name == "name"
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "lower"

    def test_parse_variable_optional_after_numeric_format(self) -> None:
        """Test parsing variable with ? after numeric format (lenient positioning)."""
        variable = parse_variable("index:03?")

        assert variable.info.name == "index"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], NumericPaddingFormat)
        assert variable.format_specs[0].width == 3

    def test_parse_variable_optional_after_separator(self) -> None:
        """Test parsing variable with ? after separator format (lenient positioning)."""
        variable = parse_variable("name:_?")

        assert variable.info.name == "name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "_"

    def test_parse_variable_optional_after_transformation(self) -> None:
        """Test parsing variable with ? after transformation format (lenient positioning)."""
        variable = parse_variable("name:lower?")

        assert variable.info.name == "name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], LowerCaseFormat)

    def test_parse_variable_optional_after_multiple_formats(self) -> None:
        """Test parsing variable with ? after chain of formats (lenient positioning)."""
        variable = parse_variable("name:03:_?")

        assert variable.info.name == "name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 2
        assert isinstance(variable.format_specs[0], NumericPaddingFormat)
        assert isinstance(variable.format_specs[1], SeparatorFormat)

    def test_parse_variable_double_optional_markers(self) -> None:
        """Test parsing variable with ? after name AND format (redundant but valid)."""
        variable = parse_variable("name?:03?")

        assert variable.info.name == "name"
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], NumericPaddingFormat)

    def test_parse_variable_optional_not_at_end(self) -> None:
        """Test parsing variable with ? in middle of format chain (treated as literal)."""
        variable = parse_variable("name:foo?:bar")

        assert variable.info.name == "name"
        assert variable.info.is_required is True  # Not at end, so not optional
        assert len(variable.format_specs) == 2
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "foo?"  # ? is part of separator
        assert isinstance(variable.format_specs[1], SeparatorFormat)
        assert variable.format_specs[1].separator == "bar"

    def test_parse_variable_quoted_question_mark_literal(self) -> None:
        """Test parsing variable with quoted ? (literal, not optional marker)."""
        variable = parse_variable("name:'?'")

        assert variable.info.name == "name"
        assert variable.info.is_required is True  # Quoted ? is literal
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "?"

    def test_parse_variable_quoted_format_with_question_mark(self) -> None:
        """Test parsing variable with quoted format containing ? (literal)."""
        variable = parse_variable("name:'foo?'")

        assert variable.info.name == "name"
        assert variable.info.is_required is True  # Quoted, so required
        assert len(variable.format_specs) == 1
        assert isinstance(variable.format_specs[0], SeparatorFormat)
        assert variable.format_specs[0].separator == "foo?"


class TestMacroParserParse:
    """Test cases for ParsedMacro() method."""

    def test_parse_simple_template_with_single_variable(self) -> None:
        """Test parsing simple template with one variable."""
        parsed = ParsedMacro("{file_name}")

        assert parsed.template == "{file_name}"
        assert len(parsed.segments) == 1
        assert isinstance(parsed.segments[0], ParsedVariable)
        assert parsed.segments[0].info.name == "file_name"

    def test_parse_template_with_static_and_variable(self) -> None:
        """Test parsing template with static text and variable."""
        parsed = ParsedMacro("inputs/{file_name}")

        assert parsed.template == "inputs/{file_name}"
        assert len(parsed.segments) == 2
        assert isinstance(parsed.segments[0], ParsedStaticValue)
        assert parsed.segments[0].text == "inputs/"
        assert isinstance(parsed.segments[1], ParsedVariable)
        assert parsed.segments[1].info.name == "file_name"

    def test_parse_template_with_multiple_variables(self) -> None:
        """Test parsing template with multiple variables."""
        parsed = ParsedMacro("{inputs}/{workflow_name?:_}{file_name}")

        assert len(parsed.segments) == 4
        assert isinstance(parsed.segments[0], ParsedVariable)
        assert parsed.segments[0].info.name == "inputs"
        assert isinstance(parsed.segments[1], ParsedStaticValue)
        assert parsed.segments[1].text == "/"
        assert isinstance(parsed.segments[2], ParsedVariable)
        assert parsed.segments[2].info.name == "workflow_name"
        assert parsed.segments[2].info.is_required is False
        assert isinstance(parsed.segments[3], ParsedVariable)
        assert parsed.segments[3].info.name == "file_name"

    def test_parse_template_with_adjacent_variables(self) -> None:
        """Test parsing template with adjacent variables (no static text between)."""
        parsed = ParsedMacro("{workflow_name}{file_name}")

        assert len(parsed.segments) == 2
        assert isinstance(parsed.segments[0], ParsedVariable)
        assert parsed.segments[0].info.name == "workflow_name"
        assert isinstance(parsed.segments[1], ParsedVariable)
        assert parsed.segments[1].info.name == "file_name"

    def test_parse_template_with_format_specs(self) -> None:
        """Test parsing template with format specifiers."""
        parsed = ParsedMacro("{outputs}/{file_name:slug}_{index:03}")

        assert len(parsed.segments) == 5
        # outputs variable
        assert isinstance(parsed.segments[0], ParsedVariable)
        assert parsed.segments[0].info.name == "outputs"
        # "/" static
        assert isinstance(parsed.segments[1], ParsedStaticValue)
        assert parsed.segments[1].text == "/"
        # file_name with slug format
        assert isinstance(parsed.segments[2], ParsedVariable)
        assert parsed.segments[2].info.name == "file_name"
        assert len(parsed.segments[2].format_specs) == 1
        assert isinstance(parsed.segments[2].format_specs[0], SlugFormat)
        # "_" static
        assert isinstance(parsed.segments[3], ParsedStaticValue)
        assert parsed.segments[3].text == "_"
        # index with numeric padding
        assert isinstance(parsed.segments[4], ParsedVariable)
        assert parsed.segments[4].info.name == "index"
        assert len(parsed.segments[4].format_specs) == 1
        assert isinstance(parsed.segments[4].format_specs[0], NumericPaddingFormat)

    def test_parse_empty_template(self) -> None:
        """Test parsing empty template returns empty static value."""
        parsed = ParsedMacro("")

        assert parsed.template == ""
        assert len(parsed.segments) == 1
        assert isinstance(parsed.segments[0], ParsedStaticValue)
        assert parsed.segments[0].text == ""

    def test_parse_static_only_template(self) -> None:
        """Test parsing template with only static text."""
        parsed = ParsedMacro("static/path/only")

        assert len(parsed.segments) == 1
        assert isinstance(parsed.segments[0], ParsedStaticValue)
        assert parsed.segments[0].text == "static/path/only"

    def test_parse_nested_braces_fails(self) -> None:
        """Test parsing template with nested braces fails."""
        from griptape_nodes.common.macro_parser import MacroSyntaxError

        with pytest.raises(MacroSyntaxError, match="Nested braces are not allowed"):
            ParsedMacro("{outer{inner}}")

    def test_parse_unclosed_brace_fails(self) -> None:
        """Test parsing template with unclosed brace fails."""
        from griptape_nodes.common.macro_parser import MacroSyntaxError

        with pytest.raises(MacroSyntaxError, match="Unclosed brace"):
            ParsedMacro("{file_name")

    def test_parse_unmatched_closing_brace_fails(self) -> None:
        """Test parsing template with unmatched closing brace fails."""
        from griptape_nodes.common.macro_parser import MacroSyntaxError

        with pytest.raises(MacroSyntaxError, match="Unmatched closing brace"):
            ParsedMacro("file_name}")

    def test_parse_empty_variable_fails(self) -> None:
        """Test parsing template with empty variable fails."""
        from griptape_nodes.common.macro_parser import MacroSyntaxError

        with pytest.raises(MacroSyntaxError, match="Empty variable"):
            ParsedMacro("{}")


class TestMacroParserFindMatchesDetailed:
    """Test cases for MacroParser.find_matches_detailed() method."""

    @pytest.fixture
    def mock_secrets_manager(self) -> Any:
        """Create a mock SecretsManager for testing."""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.get_secret.return_value = None
        return mock

    def test_find_matches_static_only_exact_match(self, mock_secrets_manager: Any) -> None:
        """Test matching static-only template with exact match."""
        parsed = ParsedMacro("static/path/only")
        result = parsed.find_matches_detailed("static/path/only", {}, mock_secrets_manager)

        assert result is not None
        assert result == {}  # No variables to extract

    def test_find_matches_static_only_no_match(self, mock_secrets_manager: Any) -> None:
        """Test matching static-only template with no match."""
        parsed = ParsedMacro("static/path/only")
        result = parsed.find_matches_detailed("different/path", {}, mock_secrets_manager)

        assert result is None

    def test_find_matches_single_unknown_variable(self, mock_secrets_manager: Any) -> None:
        """Test matching with single unknown variable."""
        from griptape_nodes.common.macro_parser import VariableInfo

        parsed = ParsedMacro("{file_name}")
        result = parsed.find_matches_detailed("image.jpg", {}, mock_secrets_manager)

        assert result is not None
        assert VariableInfo(name="file_name", is_required=True) in result
        assert result[VariableInfo(name="file_name", is_required=True)] == "image.jpg"

    def test_find_matches_with_known_variable(self, mock_secrets_manager: Any) -> None:
        """Test matching with known variable provided."""
        from griptape_nodes.common.macro_parser import VariableInfo

        parsed = ParsedMacro("{inputs}/{file_name}")
        result = parsed.find_matches_detailed("inputs/image.jpg", {"inputs": "inputs"}, mock_secrets_manager)

        assert result is not None
        # Both inputs and file_name should be in results
        assert VariableInfo(name="inputs", is_required=True) in result
        assert VariableInfo(name="file_name", is_required=True) in result
        assert result[VariableInfo(name="inputs", is_required=True)] == "inputs"
        assert result[VariableInfo(name="file_name", is_required=True)] == "image.jpg"

    def test_find_matches_known_variable_mismatch(self, mock_secrets_manager: Any) -> None:
        """Test matching fails when known variable doesn't match path."""
        parsed = ParsedMacro("{inputs}/{file_name}")
        result = parsed.find_matches_detailed("outputs/image.jpg", {"inputs": "inputs"}, mock_secrets_manager)

        assert result is None

    def test_find_matches_multiple_unknowns_with_delimiters(self, mock_secrets_manager: Any) -> None:
        """Test matching multiple unknown variables separated by static text."""
        from griptape_nodes.common.macro_parser import VariableInfo

        parsed = ParsedMacro("{dir}/{file_name}")
        result = parsed.find_matches_detailed("inputs/image.jpg", {}, mock_secrets_manager)

        assert result is not None
        assert result[VariableInfo(name="dir", is_required=True)] == "inputs"
        assert result[VariableInfo(name="file_name", is_required=True)] == "image.jpg"

    def test_find_matches_with_numeric_padding_format(self, mock_secrets_manager: Any) -> None:
        """Test matching with numeric padding format spec reversal."""
        from griptape_nodes.common.macro_parser import VariableInfo

        parsed = ParsedMacro("{file_name}_{index:03}")
        result = parsed.find_matches_detailed("render_005", {}, mock_secrets_manager)

        assert result is not None
        assert result[VariableInfo(name="file_name", is_required=True)] == "render"
        assert result[VariableInfo(name="index", is_required=True)] == 5  # Reversed to int

    def test_find_matches_empty_path(self, mock_secrets_manager: Any) -> None:
        """Test matching empty path against empty template."""
        parsed = ParsedMacro("")
        result = parsed.find_matches_detailed("", {}, mock_secrets_manager)

        assert result is not None
        assert result == {}


class TestMacroResolverResolve:
    """Test cases for parsed.resolve() method."""

    @pytest.fixture
    def mock_secrets_manager(self) -> Any:
        """Create a mock SecretsManager for testing."""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.get_secret.return_value = None
        return mock

    def test_resolve_simple_variable(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with single variable."""
        parsed = ParsedMacro("{file_name}")
        result = parsed.resolve({"file_name": "image.jpg"}, mock_secrets_manager)

        assert result == "image.jpg"

    def test_resolve_static_and_variable(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with static text and variable."""
        parsed = ParsedMacro("inputs/{file_name}")
        result = parsed.resolve({"file_name": "image.jpg"}, mock_secrets_manager)

        assert result == "inputs/image.jpg"

    def test_resolve_multiple_variables(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with multiple variables."""
        parsed = ParsedMacro("{inputs}/{workflow_name}/{file_name}")
        result = parsed.resolve(
            {"inputs": "inputs", "workflow_name": "my_workflow", "file_name": "image.jpg"}, mock_secrets_manager
        )

        assert result == "inputs/my_workflow/image.jpg"

    def test_resolve_optional_variable_present(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with optional variable that is provided."""
        parsed = ParsedMacro("{inputs}/{workflow_name?:_}{file_name}")
        result = parsed.resolve(
            {"inputs": "inputs", "workflow_name": "my_workflow", "file_name": "image.jpg"}, mock_secrets_manager
        )

        assert result == "inputs/my_workflow_image.jpg"

    def test_resolve_optional_variable_missing(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with optional variable that is not provided."""
        parsed = ParsedMacro("{inputs}/{workflow_name?:_}{file_name}")
        result = parsed.resolve({"inputs": "inputs", "file_name": "image.jpg"}, mock_secrets_manager)

        assert result == "inputs/image.jpg"

    def test_resolve_with_numeric_padding(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with numeric padding format spec."""
        parsed = ParsedMacro("{outputs}/{file_name}_{index:03}")
        result = parsed.resolve({"outputs": "outputs", "file_name": "render", "index": 5}, mock_secrets_manager)

        assert result == "outputs/render_005"

    def test_resolve_with_slug_format(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with slug format spec."""
        parsed = ParsedMacro("{outputs}/{file_name:slug}")
        result = parsed.resolve({"outputs": "outputs", "file_name": "My Cool File Name!"}, mock_secrets_manager)

        assert result == "outputs/my-cool-file-name"

    def test_resolve_with_case_formats(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with case format specs."""
        parsed_lower = ParsedMacro("{name:lower}")
        result_lower = parsed_lower.resolve({"name": "MyFile"}, mock_secrets_manager)
        assert result_lower == "myfile"

        parsed_upper = ParsedMacro("{name:upper}")
        result_upper = parsed_upper.resolve({"name": "MyFile"}, mock_secrets_manager)
        assert result_upper == "MYFILE"

    def test_resolve_with_multiple_format_specs(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with multiple chained format specs."""
        parsed = ParsedMacro("{workflow_name?:slug:_}{file_name}")
        result = parsed.resolve({"workflow_name": "My Workflow", "file_name": "image.jpg"}, mock_secrets_manager)

        assert result == "my-workflow_image.jpg"

    def test_resolve_required_variable_missing_fails(self, mock_secrets_manager: Any) -> None:
        """Test resolving template fails when required variable is missing."""
        from griptape_nodes.common.macro_parser import MacroResolutionError

        parsed = ParsedMacro("{inputs}/{file_name}")

        with pytest.raises(MacroResolutionError, match="Cannot fully resolve macro - missing required variables"):
            parsed.resolve({"inputs": "inputs"}, mock_secrets_manager)

    def test_resolve_env_var(self) -> None:
        """Test resolving template with environment variable reference."""
        from unittest.mock import MagicMock

        mock_secrets = MagicMock()
        mock_secrets.get_secret.return_value = "/path/to/outputs"

        parsed = ParsedMacro("{outputs}/{file_name}")
        result = parsed.resolve({"outputs": "$TEST_OUTPUT_DIR", "file_name": "image.jpg"}, mock_secrets)

        assert result == "/path/to/outputs/image.jpg"
        mock_secrets.get_secret.assert_called_once_with("TEST_OUTPUT_DIR", should_error_on_not_found=False)

    def test_resolve_env_var_missing_fails(self) -> None:
        """Test resolving template fails when env var is not found."""
        from unittest.mock import MagicMock

        from griptape_nodes.common.macro_parser import MacroResolutionError

        mock_secrets = MagicMock()
        mock_secrets.get_secret.return_value = None

        parsed = ParsedMacro("{outputs}/{file_name}")

        with pytest.raises(MacroResolutionError, match="Environment variable 'NONEXISTENT_VAR' not found"):
            parsed.resolve({"outputs": "$NONEXISTENT_VAR", "file_name": "image.jpg"}, mock_secrets)

    def test_resolve_static_only_template(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with only static text."""
        parsed = ParsedMacro("static/path/only")
        result = parsed.resolve({}, mock_secrets_manager)

        assert result == "static/path/only"

    def test_resolve_empty_template(self, mock_secrets_manager: Any) -> None:
        """Test resolving empty template."""
        parsed = ParsedMacro("")
        result = parsed.resolve({}, mock_secrets_manager)

        assert result == ""

    def test_resolve_integer_value(self, mock_secrets_manager: Any) -> None:
        """Test resolving template with integer value (no format spec)."""
        parsed = ParsedMacro("{count}")
        result = parsed.resolve({"count": 42}, mock_secrets_manager)

        assert result == "42"


class TestMacroFailureTypes:
    """Test cases for macro failure dataclasses."""

    def test_macro_match_failure_creation(self) -> None:
        """Test creating MacroMatchFailure with all fields."""
        failure = MacroMatchFailure(
            failure_reason=MacroMatchFailureReason.STATIC_TEXT_MISMATCH,
            expected_pattern="{inputs}/{file_name}",
            known_variables_used={"inputs": "outputs"},
            error_details="Static segment mismatch: expected 'inputs/' but found 'outputs/'",
        )

        assert failure.failure_reason == MacroMatchFailureReason.STATIC_TEXT_MISMATCH
        assert failure.expected_pattern == "{inputs}/{file_name}"
        assert failure.known_variables_used == {"inputs": "outputs"}
        assert "Static segment mismatch" in failure.error_details

    def test_macro_match_failure_invalid_syntax(self) -> None:
        """Test MacroMatchFailure with INVALID_MACRO_SYNTAX reason."""
        failure = MacroMatchFailure(
            failure_reason=MacroMatchFailureReason.INVALID_MACRO_SYNTAX,
            expected_pattern="{inputs}/{file_name",
            known_variables_used={},
            error_details="Unbalanced braces in macro schema",
        )

        assert failure.failure_reason == MacroMatchFailureReason.INVALID_MACRO_SYNTAX
        assert failure.expected_pattern == "{inputs}/{file_name"
        assert failure.known_variables_used == {}

    def test_macro_parse_failure_creation(self) -> None:
        """Test creating MacroParseFailure with all fields."""
        failure = MacroParseFailure(
            failure_reason=MacroParseFailureReason.UNCLOSED_BRACE,
            error_position=15,
            error_details="Missing closing brace after position 15",
        )

        assert failure.failure_reason == MacroParseFailureReason.UNCLOSED_BRACE
        assert failure.error_position == 15
        assert "Missing closing brace" in failure.error_details

    def test_macro_parse_failure_no_position(self) -> None:
        """Test MacroParseFailure when error position is unknown."""
        failure = MacroParseFailure(
            failure_reason=MacroParseFailureReason.UNEXPECTED_SEGMENT_TYPE,
            error_position=None,
            error_details="General syntax error",
        )

        assert failure.failure_reason == MacroParseFailureReason.UNEXPECTED_SEGMENT_TYPE
        assert failure.error_position is None

    def test_macro_match_failure_reason_values(self) -> None:
        """Test MacroMatchFailureReason enum values."""
        assert MacroMatchFailureReason.STATIC_TEXT_MISMATCH == "STATIC_TEXT_MISMATCH"
        assert MacroMatchFailureReason.DELIMITER_NOT_FOUND == "DELIMITER_NOT_FOUND"
        assert MacroMatchFailureReason.FORMAT_REVERSAL_FAILED == "FORMAT_REVERSAL_FAILED"
        assert MacroMatchFailureReason.INVALID_MACRO_SYNTAX == "INVALID_MACRO_SYNTAX"
        assert len(MacroMatchFailureReason) == 4

    def test_macro_parse_failure_reason_values(self) -> None:
        """Test MacroParseFailureReason enum values."""
        assert MacroParseFailureReason.UNMATCHED_CLOSING_BRACE == "UNMATCHED_CLOSING_BRACE"
        assert MacroParseFailureReason.UNCLOSED_BRACE == "UNCLOSED_BRACE"
        assert MacroParseFailureReason.NESTED_BRACES == "NESTED_BRACES"
        assert MacroParseFailureReason.EMPTY_VARIABLE == "EMPTY_VARIABLE"
        assert MacroParseFailureReason.UNEXPECTED_SEGMENT_TYPE == "UNEXPECTED_SEGMENT_TYPE"
        assert MacroParseFailureReason.MULTIPLE_SEQUENCE_SLOTS == "MULTIPLE_SEQUENCE_SLOTS"
        assert MacroParseFailureReason.EMPTY_LEADING_SEPARATOR == "EMPTY_LEADING_SEPARATOR"
        assert MacroParseFailureReason.MULTIPLE_LEADING_SEPARATORS == "MULTIPLE_LEADING_SEPARATORS"
        assert len(MacroParseFailureReason) == 8

    def test_macro_resolution_failure_dataclass(self) -> None:
        """Test creating MacroResolutionFailure with all fields."""
        failure = MacroResolutionFailure(
            failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
            variable_name="workflow_name",
            missing_variables={"workflow_name", "project_id"},
            error_details="Required variables not provided",
        )

        assert failure.failure_reason == MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES
        assert failure.variable_name == "workflow_name"
        assert failure.missing_variables == {"workflow_name", "project_id"}
        assert "Required variables" in failure.error_details

    def test_macro_resolution_failure_reason_values(self) -> None:
        """Test MacroResolutionFailureReason enum values."""
        assert MacroResolutionFailureReason.NUMERIC_PADDING_ON_NON_NUMERIC == "NUMERIC_PADDING_ON_NON_NUMERIC"
        assert MacroResolutionFailureReason.INVALID_INTEGER_PARSE == "INVALID_INTEGER_PARSE"
        assert MacroResolutionFailureReason.DATE_FORMAT_NOT_IMPLEMENTED == "DATE_FORMAT_NOT_IMPLEMENTED"
        assert MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES == "MISSING_REQUIRED_VARIABLES"
        assert MacroResolutionFailureReason.ENVIRONMENT_VARIABLE_NOT_FOUND == "ENVIRONMENT_VARIABLE_NOT_FOUND"
        assert MacroResolutionFailureReason.UNEXPECTED_SEGMENT_TYPE == "UNEXPECTED_SEGMENT_TYPE"
        assert len(MacroResolutionFailureReason) == 6


class TestEnhancedExceptions:
    """Test enhanced exception types with structured fields."""

    def test_macro_syntax_error_with_structured_fields(self) -> None:
        """Test MacroSyntaxError carries structured error information."""
        with pytest.raises(MacroSyntaxError) as exc_info:
            ParsedMacro("{inputs}/{file_name")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.UNCLOSED_BRACE
        assert err.error_position is not None
        assert "Unclosed brace" in str(err)

    def test_macro_syntax_error_unmatched_closing_brace(self) -> None:
        """Test MacroSyntaxError for unmatched closing brace."""
        with pytest.raises(MacroSyntaxError) as exc_info:
            ParsedMacro("{inputs}/}file_name}")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.UNMATCHED_CLOSING_BRACE
        assert err.error_position is not None

    def test_macro_syntax_error_nested_braces(self) -> None:
        """Test MacroSyntaxError for nested braces."""
        with pytest.raises(MacroSyntaxError) as exc_info:
            ParsedMacro("{outer_{inner}}")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.NESTED_BRACES
        assert err.error_position is not None

    def test_macro_syntax_error_empty_variable(self) -> None:
        """Test MacroSyntaxError for empty variable."""
        with pytest.raises(MacroSyntaxError) as exc_info:
            ParsedMacro("{inputs}/{}")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.EMPTY_VARIABLE
        assert err.error_position is not None

    def test_macro_resolution_error_missing_variables(self) -> None:
        """Test MacroResolutionError for missing required variables."""
        from unittest.mock import Mock

        macro = ParsedMacro("{workflow_name}/{file_name}")
        secrets_manager = Mock()

        with pytest.raises(MacroResolutionError) as exc_info:
            macro.resolve({"file_name": "test.txt"}, secrets_manager)

        err = exc_info.value
        assert err.failure_reason == MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES
        assert err.missing_variables == {"workflow_name"}
        assert "workflow_name" in str(err)


class TestParseSequenceSlotShorthand:
    """Test cases for `{###}` / `{##?}`-style sequence-slot syntax (issue #4902).

    The shorthand desugars to a synthesized ``ParsedVariable`` carrying a
    ``SequenceFormat`` marker. Downstream code (OSManager seed/walk,
    ScanSequencesRequest enumeration) recognizes the marker to identify
    sequence slots.

    The sigil lives **inside** the variable braces (`{###}`) rather than as
    bare static text. Reasons:
    - Markdown templates use ``#`` for headers; embedding our sigil inside
      ``{}`` avoids forcing macro authors to escape every `#` in
      doc-rendering contexts.
    - The existing grammar already says "anything in `{}` is a variable
      slot"; this just extends what variable shapes are recognized.
    - The optional form `{##?}` falls out of the trailing-`?` rule
      naturally — no separate grammar.
    """

    def test_braced_triple_hash_emits_sequence_variable_with_min_width_3(self) -> None:
        """`{###}` parses to a single ParsedVariable with SequenceFormat(min_width=3).

        Locks the canonical shape: name is the well-known ``_index`` (so
        downstream reverse-match against existing files via
        ``extract_variables`` binds the integer under a stable key), the
        variable is required, and the lone format spec is ``SequenceFormat``
        — NOT ``NumericPaddingFormat``. That distinction matters because
        OSManager queries the format-spec type to tell user-bound
        ``{shot:03}`` apart from a sequence slot.
        """
        segments = parse_segments("my_workflow_v{###}.py")

        assert len(segments) == 3
        assert isinstance(segments[0], ParsedStaticValue)
        assert segments[0].text == "my_workflow_v"
        assert isinstance(segments[1], ParsedVariable)
        assert segments[1].info.name == SEQUENCE_VARIABLE_NAME
        assert segments[1].info.is_required is True
        assert len(segments[1].format_specs) == 1
        spec = segments[1].format_specs[0]
        assert isinstance(spec, SequenceFormat)
        assert spec.min_width == 3
        # No NumericPaddingFormat involvement on the `{###}` path — see #4902
        # for why these are kept as distinct format types.
        assert not any(isinstance(s, NumericPaddingFormat) for s in segments[1].format_specs)
        assert isinstance(segments[2], ParsedStaticValue)
        assert segments[2].text == ".py"

    def test_braced_double_hash_question_mark_emits_optional_sequence_slot(self) -> None:
        """`{##?}` parses as an optional sequence slot with min_width=2.

        The trailing `?` re-uses the existing optional-variable convention.
        Optional sequence slots are what most file-save situations want
        (`save_file`, `save_node_output`, etc. where the first save lands
        without an index and only collisions trigger indexing).
        """
        segments = parse_segments("foo{##?}.png")

        var_segments = [s for s in segments if isinstance(s, ParsedVariable)]
        assert len(var_segments) == 1
        var = var_segments[0]
        assert var.info.name == SEQUENCE_VARIABLE_NAME
        assert var.info.is_required is False
        assert len(var.format_specs) == 1
        spec = var.format_specs[0]
        assert isinstance(spec, SequenceFormat)
        assert spec.min_width == 2

    @pytest.mark.parametrize(
        ("hash_run", "expected_min_width"),
        [("#", 1), ("##", 2), ("####", 4), ("#####", 5)],
    )
    def test_hash_run_lengths_map_to_min_width(self, hash_run: str, expected_min_width: int) -> None:
        """Any run length of `#` characters inside braces becomes that exact min_width."""
        segments = parse_segments(f"v{{{hash_run}}}.py")

        sequence_vars = [s for s in segments if isinstance(s, ParsedVariable)]
        assert len(sequence_vars) == 1
        spec = sequence_vars[0].format_specs[0]
        assert isinstance(spec, SequenceFormat)
        assert spec.min_width == expected_min_width

    @pytest.mark.parametrize(
        ("hash_run", "expected_min_width"),
        [("#", 1), ("##", 2), ("####", 4), ("#####", 5)],
    )
    def test_optional_hash_run_lengths_map_to_min_width(self, hash_run: str, expected_min_width: int) -> None:
        """Any run length followed by `?` produces the same min_width with is_required=False."""
        segments = parse_segments(f"v{{{hash_run}?}}.py")

        sequence_vars = [s for s in segments if isinstance(s, ParsedVariable)]
        assert len(sequence_vars) == 1
        var = sequence_vars[0]
        assert var.info.is_required is False
        spec = var.format_specs[0]
        assert isinstance(spec, SequenceFormat)
        assert spec.min_width == expected_min_width

    def test_braced_hash_run_adjacent_to_brace_variable_parses_cleanly(self) -> None:
        """`prefix_{###}{ext}` splits into static, sequence slot, then variable.

        Confirms the tokenizer handles back-to-back ``{}`` blocks where the
        first is a sequence-slot shorthand and the second is a regular
        variable. No static text gets eaten or duplicated between them.
        """
        segments = parse_segments("prefix_{###}{ext}")

        assert len(segments) == 3
        assert isinstance(segments[0], ParsedStaticValue)
        assert segments[0].text == "prefix_"
        assert isinstance(segments[1], ParsedVariable)
        assert segments[1].info.name == SEQUENCE_VARIABLE_NAME
        assert isinstance(segments[2], ParsedVariable)
        assert segments[2].info.name == "ext"

    def test_two_sequence_slots_in_one_macro_raises_multiple_sequence_slots(self) -> None:
        """Two `{###}` blocks in the same template raise MULTIPLE_SEQUENCE_SLOTS.

        OSManager would have no way to pick which slot to auto-fill, so the
        parser rejects this upfront with a specific failure reason. The
        check happens after the per-block parse completes (post-pass walk
        over the segment list).
        """
        with pytest.raises(MacroSyntaxError) as exc_info:
            parse_segments("v{###}_take_{##}.png")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.MULTIPLE_SEQUENCE_SLOTS

    def test_two_sequence_slots_required_and_optional_also_raises(self) -> None:
        """Mixing required and optional sequence slots in one macro still raises.

        Defense against the `{###}_{##?}` case — both are sequence slots
        even though only one is optional. OSManager still can't pick.
        """
        with pytest.raises(MacroSyntaxError) as exc_info:
            parse_segments("{###}_{##?}.png")

        err = exc_info.value
        assert err.failure_reason == MacroParseFailureReason.MULTIPLE_SEQUENCE_SLOTS

    def test_explicit_index_padding_still_parses_as_numeric_padding(self) -> None:
        """`{_index:03}` remains the user-bound rendering form — no SequenceFormat involved.

        The explicit-padding path stays a string-formatting concern. Only
        `{###}` shorthand creates a sequence slot; `{_index:03}` continues
        to parse as a plain padded variable. OSManager still treats it as
        a sequence slot via the legacy ``NumericPaddingFormat`` heuristic
        (the OR-branch documented on ``_has_sequence_slot_marker``), but
        that's an OSManager concern — the parser's output stays cleanly
        separated.
        """
        segments = parse_segments("v{_index:03}.py")

        var_segments = [s for s in segments if isinstance(s, ParsedVariable)]
        assert len(var_segments) == 1
        var = var_segments[0]
        assert var.info.name == "_index"
        assert any(isinstance(s, NumericPaddingFormat) for s in var.format_specs)
        assert not any(isinstance(s, SequenceFormat) for s in var.format_specs)

    def test_bare_hash_run_in_static_text_is_literal(self) -> None:
        """Bare `###` outside `{}` is literal static text, NOT a sequence slot.

        Pins the moved-to-braces decision: macro authors using `#` chars in
        their markdown or static text don't accidentally trigger sequence
        semantics. To get a sequence slot you must wrap the `#`s in `{}`.
        """
        segments = parse_segments("foo###.png")

        # Pure static text — no synthesized variable.
        assert len(segments) == 1
        assert isinstance(segments[0], ParsedStaticValue)
        assert segments[0].text == "foo###.png"

    def test_braced_hash_only_template_parses(self) -> None:
        """A template that is *only* `{###}` (no static prefix/suffix) parses to one variable."""
        segments = parse_segments("{###}")

        assert len(segments) == 1
        assert isinstance(segments[0], ParsedVariable)
        spec = segments[0].format_specs[0]
        assert isinstance(spec, SequenceFormat)
        assert spec.min_width == 3

    def test_template_ending_with_brace_emits_no_trailing_empty_static(self) -> None:
        """`{var}` (template ending with the closing brace) emits only the variable, no trailing static.

        Confirms ``parse_segments`` doesn't append an empty ``ParsedStaticValue``
        when the template ends exactly at a closing brace.
        """
        segments = parse_segments("{prefix}")

        # Single variable segment; no trailing empty static.
        assert len(segments) == 1
        assert isinstance(segments[0], ParsedVariable)
        assert segments[0].info.name == "prefix"

    def test_full_resolve_with_sequence_slot_uses_min_width(self) -> None:
        """End-to-end: ParsedMacro.resolve() with a `{###}` slot zero-pads correctly.

        Confirms the integration between the parser's synthesized variable
        and the resolver. Caller binds the synthesized ``_index`` name to
        an integer; the renderer zero-pads to min_width or overflows
        naturally past it.
        """
        from unittest.mock import MagicMock

        macro = ParsedMacro("v{###}.py")
        secrets_manager = MagicMock()

        assert macro.resolve({SEQUENCE_VARIABLE_NAME: 1}, secrets_manager) == "v001.py"
        assert macro.resolve({SEQUENCE_VARIABLE_NAME: 42}, secrets_manager) == "v042.py"
        assert macro.resolve({SEQUENCE_VARIABLE_NAME: 1000}, secrets_manager) == "v1000.py"


class TestLeadingSeparatorSyntax:
    """Parser + resolve + reverse-match tests for `{var?:^prefix}` (issue #5023).

    The leading separator is a `FormatSpec` (``LeadingSeparatorFormat``) marked
    by a ``^`` at the start of its format-spec text. Prepends the prefix to the
    variable's rendered value iff the variable emits; the optional-variable
    omit path erases the whole segment (including this spec) with no work in
    the resolver.

    Enforces two position invariants at parse time so later specs can't mangle
    the prefix: at most one per variable, and it must be the last spec.
    """

    # ----- Parser -----

    def test_parse_leading_separator_basic(self) -> None:
        """`{shot:^_v}` parses to a variable ending in `LeadingSeparatorFormat("_v")`."""
        variable = parse_variable("shot:^_v")

        assert variable.info.name == "shot"
        assert variable.info.is_required is True
        assert variable.format_specs == [LeadingSeparatorFormat(prefix="_v")]
        assert variable.default_value is None

    def test_parse_leading_separator_optional(self) -> None:
        """`{shot?:^_v}` preserves the leading separator and marks optional."""
        variable = parse_variable("shot?:^_v")

        assert variable.info.name == "shot"
        assert variable.info.is_required is False
        assert variable.format_specs == [LeadingSeparatorFormat(prefix="_v")]

    def test_parse_leading_separator_composes_with_sequence_shorthand(self) -> None:
        """`{###?:^_v}` — sequence-slot shorthand carries a trailing leading-separator spec.

        The parser now splits on ``:`` before running the shorthand regex so the
        two grammars compose. `SequenceFormat` is first, `LeadingSeparatorFormat`
        is last — the position invariant that lets ordering "just work".
        """
        variable = parse_variable("###?:^_v")

        assert variable.info.name == SEQUENCE_VARIABLE_NAME
        assert variable.info.is_required is False
        assert variable.format_specs == [SequenceFormat(min_width=3), LeadingSeparatorFormat(prefix="_v")]

    def test_parse_leading_separator_after_numeric_padding(self) -> None:
        """`{shot:03:^_v}` composes numeric padding + leading separator."""
        variable = parse_variable("shot:03:^_v")

        assert variable.format_specs == [NumericPaddingFormat(width=3), LeadingSeparatorFormat(prefix="_v")]

    def test_parse_leading_separator_after_trailing_separator(self) -> None:
        """`{shot?:_:^v_}` — trailing separator first, leading separator last.

        Trailing separator's "must be first" convention plus leading separator's
        "must be last" invariant place them at opposite ends of `format_specs`,
        which is exactly the mental model authors read from the syntax.
        """
        variable = parse_variable("shot?:_:^v_")

        assert variable.info.is_required is False
        assert variable.format_specs == [SeparatorFormat(separator="_"), LeadingSeparatorFormat(prefix="v_")]

    def test_parse_leading_separator_after_case_transform(self) -> None:
        """`{shot:upper:^V}` — case transform first, leading separator last."""
        variable = parse_variable("shot:upper:^V")

        assert isinstance(variable.format_specs[0], UpperCaseFormat)
        assert variable.format_specs[1] == LeadingSeparatorFormat(prefix="V")

    def test_parse_empty_leading_separator_raises(self) -> None:
        """`{shot:^}` (just the caret, no payload) raises EMPTY_LEADING_SEPARATOR.

        `error_position` is `None` — `parse_format_spec` receives a spec
        string without template-relative offset context, so hardcoding `0`
        would misreport the location. Regression guard against reintroducing
        a bogus offset.
        """
        with pytest.raises(MacroSyntaxError) as exc_info:
            parse_variable("shot:^")

        assert exc_info.value.failure_reason == MacroParseFailureReason.EMPTY_LEADING_SEPARATOR
        assert exc_info.value.error_position is None

    def test_parse_multiple_leading_separators_raises(self) -> None:
        """`{shot:^a:^b}` (two `^` specs) raises MULTIPLE_LEADING_SEPARATORS.

        `error_position` is `None` — the normalizer runs after all format
        specs are collected, so the template-relative offset of the second
        `:^` isn't known at this depth. Better honest-unknown than a bogus `0`.
        """
        with pytest.raises(MacroSyntaxError) as exc_info:
            parse_variable("shot:^a:^b")

        assert exc_info.value.failure_reason == MacroParseFailureReason.MULTIPLE_LEADING_SEPARATORS
        assert exc_info.value.error_position is None

    def test_parse_leading_separator_normalized_to_end(self) -> None:
        """`{shot:^_v:upper}` — leading separator written mid-list — parses with the leading spec at the tail.

        Author-friendliness: a leading separator is semantically a prefix
        applied to the final rendered value, so `{shot:^_v:upper}` and
        `{shot:upper:^_v}` mean the same thing. The parser normalizes the
        list order (moves the `LeadingSeparatorFormat` to the end) rather
        than making the author remember the "must be last" invariant.
        """
        variable = parse_variable("shot:^_v:upper")

        assert variable.format_specs == [UpperCaseFormat(), LeadingSeparatorFormat(prefix="_v")]

    def test_parse_leading_separator_equivalent_regardless_of_position(self) -> None:
        """`{shot:^_v:upper}` and `{shot:upper:^_v}` produce the same rendered output.

        End-to-end confirmation of the normalization: no matter where the
        author writes the `:^` spec, the resolved string is identical for
        the same variable binding. Guards against a future edit that
        forgets to move the leading separator to the tail before running
        the resolver.
        """
        from unittest.mock import MagicMock

        secrets_manager = MagicMock()

        mid = ParsedMacro("{shot:^_v:upper}").resolve({"shot": "a"}, secrets_manager)
        end = ParsedMacro("{shot:upper:^_v}").resolve({"shot": "a"}, secrets_manager)

        assert mid == end == "_vA"

    def test_parse_sequence_shorthand_with_trailing_optional_marker(self) -> None:
        """`{###:upper?}` — trailing `?` on the last format spec — marks the sequence variable optional.

        Before the fix, the sequence-shorthand branch skipped the trailing-`?`
        handling that the regular-variable branch performed, so `{###:upper?}`
        would produce a required variable with `SeparatorFormat("upper?")`
        instead of an optional variable with `UpperCaseFormat`. This asserts
        the two grammars agree on trailing-`?` semantics.
        """
        variable = parse_variable("###:upper?")

        assert variable.info.name == SEQUENCE_VARIABLE_NAME
        assert variable.info.is_required is False
        assert len(variable.format_specs) == 2
        assert isinstance(variable.format_specs[0], SequenceFormat)
        assert variable.format_specs[0].min_width == 3
        assert isinstance(variable.format_specs[1], UpperCaseFormat)

    def test_parse_sequence_shorthand_trailing_and_pre_colon_optional_are_equivalent(self) -> None:
        """`{###?:upper}` and `{###:upper?}` parse to the same ``ParsedVariable``.

        Locks the grammatical symmetry between sequence-shorthand and
        regular-variable trailing-`?` handling — Collin's finding on #5026.
        """
        pre_colon = parse_variable("###?:upper")
        post_colon = parse_variable("###:upper?")

        assert pre_colon.info == post_colon.info
        assert pre_colon.format_specs == post_colon.format_specs
        assert pre_colon.default_value == post_colon.default_value

    def test_parse_sequence_shorthand_quoted_trailing_question_mark_is_literal(self) -> None:
        """`{###:'foo?'}` — quoted `?` on sequence shorthand — stays required, `?` is literal.

        Now that the sequence-shorthand branch calls the shared trailing-`?`
        helper, it needs to respect the same quoted-preserves-literal rule
        the regular-variable branch does. Guards against a future edit
        that drops the quoted-check from the helper.
        """
        variable = parse_variable("###:'foo?'")

        assert variable.info.name == SEQUENCE_VARIABLE_NAME
        assert variable.info.is_required is True  # Quoted `?` doesn't trigger optionality
        assert len(variable.format_specs) == 2
        assert isinstance(variable.format_specs[0], SequenceFormat)
        assert isinstance(variable.format_specs[1], SeparatorFormat)
        assert variable.format_specs[1].separator == "foo?"

    def test_parse_sequence_shorthand_leading_separator_with_trailing_optional(self) -> None:
        """`{###:^_v?}` composes leading separator + trailing-`?` optional marker.

        The `?` at the end of `^_v?` must strip cleanly (marking the variable
        optional) and the surviving `^_v` must parse as
        `LeadingSeparatorFormat("_v")`. Locks the composition of the two
        grammar features on the sequence-shorthand branch.
        """
        variable = parse_variable("###:^_v?")

        assert variable.info.name == SEQUENCE_VARIABLE_NAME
        assert variable.info.is_required is False
        assert variable.format_specs == [SequenceFormat(min_width=3), LeadingSeparatorFormat(prefix="_v")]

    def test_parse_regular_variable_has_no_leading_separator(self) -> None:
        """Regression check: no `^` spec present means `format_specs` has no `LeadingSeparatorFormat`."""
        variable = parse_variable("shot")

        assert not any(isinstance(spec, LeadingSeparatorFormat) for spec in variable.format_specs)

    # ----- Resolve -----

    def test_resolve_leading_separator_required_variable(self) -> None:
        """`{shot:^_v}` with `shot=5` renders as `_v5`."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("{shot:^_v}")
        secrets_manager = MagicMock()

        assert macro.resolve({"shot": 5}, secrets_manager) == "_v5"

    def test_resolve_leading_separator_optional_bound(self) -> None:
        """`{shot?:^_v}` with `shot="alpha"` renders as `_valpha`."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("a{shot?:^_v}b")
        secrets_manager = MagicMock()

        assert macro.resolve({"shot": "alpha"}, secrets_manager) == "a_valphab"

    def test_resolve_leading_separator_optional_unbound(self) -> None:
        """`{shot?:^_v}` with no `shot` renders empty — the whole segment vanishes."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("a{shot?:^_v}b")
        secrets_manager = MagicMock()

        assert macro.resolve({}, secrets_manager) == "ab"

    def test_resolve_leading_separator_with_sequence_shorthand_bound(self) -> None:
        """`render{###?:^_v}.png` with `_index=1` renders as `render_v001.png`.

        The demo case: exactly why this feature exists. `SequenceFormat.apply`
        renders "001", then `LeadingSeparatorFormat.apply` prepends "_v".
        """
        from unittest.mock import MagicMock

        macro = ParsedMacro("render{###?:^_v}.png")
        secrets_manager = MagicMock()

        assert macro.resolve({SEQUENCE_VARIABLE_NAME: 1}, secrets_manager) == "render_v001.png"

    def test_resolve_leading_separator_with_sequence_shorthand_unbound(self) -> None:
        """`render{###?:^_v}.png` with `_index` unbound renders as `render.png` (segment omitted)."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("render{###?:^_v}.png")
        secrets_manager = MagicMock()

        assert macro.resolve({}, secrets_manager) == "render.png"

    def test_resolve_leading_separator_after_numeric_padding(self) -> None:
        """`{shot:03:^_v}` with `shot=5` renders as `_v005` — padding first, then prepend."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("{shot:03:^_v}")
        secrets_manager = MagicMock()

        assert macro.resolve({"shot": 5}, secrets_manager) == "_v005"

    def test_resolve_leading_and_trailing_separators_together(self) -> None:
        """`{shot:_:^v_}` with `shot=5` renders as `v_5_` — prefix, value, suffix."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("a{shot:_:^v_}b")
        secrets_manager = MagicMock()

        assert macro.resolve({"shot": 5}, secrets_manager) == "av_5_b"

    def test_resolve_leading_separator_after_case_transform(self) -> None:
        """`{shot:upper:^V}` with `shot="a"` renders as `VA` — upper first, then prepend."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("{shot:upper:^V}")
        secrets_manager = MagicMock()

        assert macro.resolve({"shot": "a"}, secrets_manager) == "VA"

    def test_resolve_leading_separator_prose_case_bound(self) -> None:
        """Prose reminder that macros aren't just for filenames.

        Template: `Hello, {name?}!{intro?:^ Nice to meet you.}` — the leading
        separator renders " Nice to meet you." iff `intro` is bound.
        """
        from unittest.mock import MagicMock

        macro = ParsedMacro("Hello, {name?}!{intro?:^ Nice to meet you.}")
        secrets_manager = MagicMock()

        both = macro.resolve({"name": "Alice", "intro": "y"}, secrets_manager)
        assert both == "Hello, Alice! Nice to meet you.y"

    def test_resolve_leading_separator_prose_case_unbound(self) -> None:
        """Same prose template, `intro` unbound — leading separator vanishes with the segment."""
        from unittest.mock import MagicMock

        macro = ParsedMacro("Hello, {name?}!{intro?:^ Nice to meet you.}")
        secrets_manager = MagicMock()

        assert macro.resolve({"name": "Alice"}, secrets_manager) == "Hello, Alice!"

    # ----- Reverse-match -----

    def test_reverse_match_leading_separator_strips_prefix(self) -> None:
        """`_v005` reverse-matched against `{shot:03:^_v}` recovers `shot=5`.

        `reverse_format_specs` iterates in reverse order — LeadingSeparatorFormat
        strips the `_v` first, then NumericPaddingFormat parses `005` as `5`.
        """
        from unittest.mock import MagicMock

        macro = ParsedMacro("v{shot:03:^_v}.py")
        secrets_manager = MagicMock()

        extracted = macro.extract_variables("v_v005.py", {}, secrets_manager)
        assert extracted == {"shot": 5}

    def test_reverse_match_leading_separator_absent_prefix_is_idempotent(self) -> None:
        """A path missing the prefix still reverse-matches — same contract as SeparatorFormat.reverse.

        `LeadingSeparatorFormat.reverse` is idempotent when the prefix isn't
        present; the raw digits still parse as the padded value. This mirrors
        the trailing side and keeps reverse-match forgiving of ambiguous
        filenames (the caller decides whether to trust the extraction).
        """
        from unittest.mock import MagicMock

        macro = ParsedMacro("v{shot:03:^_v}.py")
        secrets_manager = MagicMock()

        # Path has `005` directly, no `_v` prefix — LeadingSeparatorFormat.reverse
        # returns the value unchanged; NumericPaddingFormat.reverse then parses it.
        extracted = macro.extract_variables("v005.py", {}, secrets_manager)
        assert extracted == {"shot": 5}
