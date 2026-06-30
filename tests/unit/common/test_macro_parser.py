"""Tests for macro parser functionality."""

# ruff: noqa: PLR2004

from typing import Any

import pytest

from griptape_nodes.common.macro_parser import (
    AbbrevFormat,
    CamelCaseFormat,
    DateFormat,
    DotCaseFormat,
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
    SlugFormat,
    SnakeCaseFormat,
    TitleCaseFormat,
    TrimFormat,
    UpperCaseFormat,
    VariableInfo,
)
from griptape_nodes.common.macro_parser.parsing import parse_variable


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
        assert len(MacroParseFailureReason) == 5

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
