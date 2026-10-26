"""Tests for function calls inside inline macro tokens ({path_join(dir, name)})."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from griptape_nodes.common.macro_parser import (
    MacroFunctionArgumentError,
    MacroFunctionArityError,
    MacroFunctionError,
    MacroFunctionSyntaxError,
    MacroPathEscapeError,
    MacroResolutionError,
)
from griptape_nodes.common.macro_parser.functions import MacroFunctions
from griptape_nodes.exe_types.variable_resolver import VariableResolver


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace folder holding a project with a temp folder."""
    root = tmp_path / "workspace"
    (root / "project" / "temp").mkdir(parents=True)
    return root


@pytest.fixture
def variables(workspace: Path) -> dict[str, str | int]:
    """Project builtins pointing into `workspace`, plus a few user variables."""
    return {
        "workspace_dir": str(workspace),
        "project_dir": str(workspace / "project"),
        "temp": str(workspace / "project" / "temp"),
        "outputs": str(workspace / "project" / "outputs"),
        "name": "shot.png",
        "count": 4,
        "text_number": "2.5",
        "shots": "a\nb\nc",
    }


def resolve(text: str, variables: dict[str, str | int]) -> str:
    """Resolve `text` the way a parameter value is resolved during a run."""
    return VariableResolver.resolve_string(text, variables)


class TestPathFunctions:
    def test_path_join(self, variables: dict[str, str | int], workspace: Path) -> None:
        expected = str(workspace / "project" / "outputs" / "renders" / "shot.png")
        assert resolve('{path_join(outputs, "renders", name)}', variables) == expected

    def test_path_join_normalises(self, variables: dict[str, str | int], workspace: Path) -> None:
        expected = str(workspace / "project" / "outputs" / "b")
        assert resolve('{path_join(outputs, "a/./../b")}', variables) == expected

    def test_ext(self, variables: dict[str, str | int]) -> None:
        assert resolve("{ext(name)}", variables) == "png"

    def test_ext_without_extension(self, variables: dict[str, str | int]) -> None:
        assert resolve('{ext("README")}', variables) == ""

    def test_with_ext(self, variables: dict[str, str | int], workspace: Path) -> None:
        expected = str(workspace / "project" / "outputs" / "shot.jpg")
        assert resolve('{with_ext(path_join(outputs, name), ".jpg")}', variables) == expected
        assert resolve('{with_ext(path_join(outputs, name), "jpg")}', variables) == expected

    def test_with_ext_empty_removes_extension(self, variables: dict[str, str | int], workspace: Path) -> None:
        expected = str(workspace / "project" / "outputs" / "shot")
        assert resolve('{with_ext(path_join(outputs, name), "")}', variables) == expected

    def test_basename(self, variables: dict[str, str | int]) -> None:
        assert resolve('{basename("renders/shot.png")}', variables) == "shot.png"

    def test_dirname(self, variables: dict[str, str | int], workspace: Path) -> None:
        assert resolve("{dirname(path_join(outputs, name))}", variables) == str(workspace / "project" / "outputs")

    def test_stem(self, variables: dict[str, str | int]) -> None:
        assert resolve('{stem("renders/shot.v002.png")}', variables) == "shot.v002"

    def test_temp_dir(self, variables: dict[str, str | int], workspace: Path) -> None:
        assert resolve("{temp_dir()}", variables) == str(workspace / "project" / "temp")

    def test_temp_file(self, variables: dict[str, str | int], workspace: Path) -> None:
        result = resolve('{temp_file("png")}', variables)
        assert Path(result).parent == workspace / "project" / "temp"
        assert re.fullmatch(r"tmp_[0-9a-f]{12}\.png", Path(result).name)

    def test_temp_file_without_extension(self, variables: dict[str, str | int]) -> None:
        assert re.fullmatch(r"tmp_[0-9a-f]{12}", Path(resolve("{temp_file()}", variables)).name)

    def test_temp_dir_without_project_is_an_error(self) -> None:
        with pytest.raises(MacroFunctionArgumentError, match="temp folder"):
            resolve("{temp_dir()}", {})


class TestPathEscape:
    def test_dot_dot_climbing_out_of_the_base_is_rejected(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroPathEscapeError) as exc_info:
            resolve('{path_join(outputs, "..", "..", "secrets")}', variables)
        assert exc_info.value.function_name == "path_join"
        assert exc_info.value.position == 1

    def test_dot_dot_rejected_without_project_context(self) -> None:
        with pytest.raises(MacroPathEscapeError):
            resolve('{path_join("renders", "../../etc")}', {})

    def test_absolute_later_part_is_rejected(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroPathEscapeError) as exc_info:
            resolve('{path_join(outputs, "/etc/passwd")}', variables)
        assert exc_info.value.function_name == "path_join"

    def test_user_supplied_absolute_base_outside_workspace_is_allowed(
        self, variables: dict[str, str | int], tmp_path: Path
    ) -> None:
        share = tmp_path / "Volumes" / "share"
        variables["share"] = str(share)
        assert resolve('{path_join(share, "shot", name)}', variables) == str(share / "shot" / "shot.png")

    def test_absolute_literal_base_outside_workspace_is_allowed(self, variables: dict[str, str | int]) -> None:
        assert resolve('{path_join("/Volumes/share", name)}', variables) == str(Path("/Volumes/share") / "shot.png")

    def test_dot_dot_inside_the_base_is_allowed(self, variables: dict[str, str | int], workspace: Path) -> None:
        expected = str(workspace / "project" / "outputs" / "b.png")
        assert resolve('{path_join(outputs, "a", "..", "b.png")}', variables) == expected

    def test_dirname_and_with_ext_outside_workspace_are_allowed(self, variables: dict[str, str | int]) -> None:
        assert resolve('{dirname("/Volumes/share/a.png")}', variables) == str(Path("/Volumes/share"))
        assert resolve('{with_ext("/Volumes/share/a.png", "exr")}', variables) == str(Path("/Volumes/share/a.exr"))

    def test_temp_dir_outside_workspace_is_rejected(self, variables: dict[str, str | int], tmp_path: Path) -> None:
        variables["temp"] = str(tmp_path / "elsewhere")
        with pytest.raises(MacroPathEscapeError, match="outside the workspace") as exc_info:
            resolve("{temp_dir()}", variables)
        assert exc_info.value.function_name == "temp_dir"
        with pytest.raises(MacroPathEscapeError):
            resolve('{temp_file("png")}', variables)

    def test_temp_dir_symlinked_out_of_workspace_is_rejected(
        self, variables: dict[str, str | int], tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = Path(str(variables["workspace_dir"])) / "temp_link"
        link.symlink_to(outside, target_is_directory=True)
        variables["temp"] = str(link)
        with pytest.raises(MacroPathEscapeError):
            resolve("{temp_dir()}", variables)

    def test_relative_path_is_anchored_to_the_workspace(self, variables: dict[str, str | int]) -> None:
        assert resolve('{path_join("renders", name)}', variables) == str(Path("renders") / "shot.png")

    def test_project_dir_outside_workspace_is_allowed(self, variables: dict[str, str | int], tmp_path: Path) -> None:
        project = tmp_path / "other_project"
        variables["project_dir"] = str(project)
        assert resolve('{path_join(project_dir, "a.png")}', variables) == str(project / "a.png")

    def test_path_escape_is_a_macro_resolution_error(self) -> None:
        assert issubclass(MacroPathEscapeError, MacroFunctionError)
        assert issubclass(MacroFunctionError, MacroResolutionError)


class TestTimeAndRandom:
    def test_now_default_format(self, variables: dict[str, str | int]) -> None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", resolve("{now()}", variables))

    def test_now_custom_format(self, variables: dict[str, str | int]) -> None:
        assert resolve('{now("%Y")}', variables) == str(datetime.now().astimezone().year)

    def test_date_default_format(self, variables: dict[str, str | int]) -> None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve("{date()}", variables))

    def test_date_custom_format(self, variables: dict[str, str | int]) -> None:
        assert re.fullmatch(r"\d{8}", resolve('{date("%Y%m%d")}', variables))

    def test_random_range(self, variables: dict[str, str | int]) -> None:
        for _ in range(50):
            assert 3 <= int(resolve("{random(3, 5)}", variables)) <= 5  # noqa: PLR2004

    def test_random_defaults(self, variables: dict[str, str | int]) -> None:
        assert 0 <= int(resolve("{random()}", variables)) <= 100  # noqa: PLR2004
        assert 0 <= int(resolve("{random(2)}", variables)) <= 2  # noqa: PLR2004

    def test_random_low_above_high_is_an_error(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError, match="greater than"):
            resolve("{random(5, 1)}", variables)

    def test_random_text(self, variables: dict[str, str | int]) -> None:
        assert re.fullmatch(r"[a-z0-9]{8}", resolve("{random_text()}", variables))
        assert re.fullmatch(r"[a-z0-9]{3}", resolve("{random_text(3)}", variables))

    def test_random_text_length_bounds(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError):
            resolve("{random_text(0)}", variables)

    def test_volatile_tokens_are_stable_inside_one_scope(self, variables: dict[str, str | int]) -> None:
        token = VariableResolver.seed_cache(variables)
        try:
            first = resolve("{random_text(16)}", variables)
            second = resolve("{random_text(16)}", variables)
        finally:
            VariableResolver.reset_cache(token)
        assert first == second

    def test_volatile_tokens_are_fresh_outside_a_scope(self, variables: dict[str, str | int]) -> None:
        assert resolve("{random_text(16)}", variables) != resolve("{random_text(16)}", variables)


class TestTextFunctions:
    def test_join_values(self, variables: dict[str, str | int]) -> None:
        assert resolve('{join("_", "a", count, name)}', variables) == "a_4_shot.png"

    def test_join_list_variable(self, variables: dict[str, str | int]) -> None:
        assert resolve('{join(", ", shots)}', variables) == "a, b, c"

    def test_format(self, variables: dict[str, str | int]) -> None:
        assert resolve('{format("shot_%s_v%s (100%%)", count, 2)}', variables) == "shot_4_v2 (100%)"

    def test_format_placeholder_count_mismatch(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError, match="placeholder"):
            resolve('{format("%s_%s", count)}', variables)

    def test_upper_lower(self, variables: dict[str, str | int]) -> None:
        assert resolve("{upper(name)}", variables) == "SHOT.PNG"
        assert resolve('{lower("ABC")}', variables) == "abc"


class TestMathFunctions:
    def test_add(self, variables: dict[str, str | int]) -> None:
        assert resolve("{add(count, 1, 2)}", variables) == "7"

    def test_add_numeric_string_variable(self, variables: dict[str, str | int]) -> None:
        assert resolve("{add(text_number, 1)}", variables) == "3.5"

    def test_sub(self, variables: dict[str, str | int]) -> None:
        assert resolve("{sub(count, 10)}", variables) == "-6"

    def test_mul(self, variables: dict[str, str | int]) -> None:
        assert resolve("{mul(count, 2, 1.5)}", variables) == "12"

    def test_div(self, variables: dict[str, str | int]) -> None:
        assert resolve("{div(count, 8)}", variables) == "0.5"
        assert resolve("{div(count, 2)}", variables) == "2"

    def test_div_by_zero(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError, match="zero"):
            resolve("{div(1, 0)}", variables)

    def test_round(self, variables: dict[str, str | int]) -> None:
        assert resolve("{round(2.6)}", variables) == "3"
        assert resolve("{round(2.345, 2)}", variables) == "2.35"

    def test_min_max(self, variables: dict[str, str | int]) -> None:
        assert resolve("{min(count, 2, 9)}", variables) == "2"
        assert resolve("{max(count, 2, 9)}", variables) == "9"

    def test_non_number_argument(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError) as exc_info:
            resolve("{add(name, 1)}", variables)
        assert exc_info.value.function_name == "add"
        assert exc_info.value.position == len("{add(")


class TestSyntax:
    def test_nesting(self, variables: dict[str, str | int]) -> None:
        assert resolve('{upper(stem(with_ext(name, "jpg")))}', variables) == "SHOT"

    def test_literals(self, variables: dict[str, str | int]) -> None:
        assert resolve("{join('-', \"a b\", 'it\\'s', -3, 1.5)}", variables) == "a b-it's--3-1.5"

    def test_commas_and_parens_inside_strings(self, variables: dict[str, str | int]) -> None:
        assert resolve('{join("", "a,b", "(c)")}', variables) == "a,b(c)"

    def test_whitespace_is_allowed(self, variables: dict[str, str | int]) -> None:
        assert resolve('{ upper ( "x" ) }', variables) == "X"

    def test_format_spec_after_call(self, variables: dict[str, str | int]) -> None:
        assert resolve('{lower("SHOT"):upper}', variables) == "SHOT"
        assert resolve("{add(count, 1):03}", variables) == "005"

    def test_mixed_with_variables_and_text(self, variables: dict[str, str | int]) -> None:
        assert resolve("v{count:03}_{stem(name)}.{ext(name)}", variables) == "v004_shot.png"

    def test_error_position_points_into_the_full_text(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArityError) as exc_info:
            resolve("prefix {upper()}", variables)
        assert exc_info.value.position == len("prefix {")
        assert "character 9" in str(exc_info.value)

    def test_unknown_function_is_left_verbatim(self, variables: dict[str, str | int]) -> None:
        assert resolve("{explode(name)}", variables) == "{explode(name)}"

    def test_unknown_nested_function_is_left_verbatim(self, variables: dict[str, str | int]) -> None:
        assert resolve("{upper(explode(name))}", variables) == "{upper(explode(name))}"

    def test_jsx_like_text_is_left_verbatim_next_to_resolved_tokens(self, variables: dict[str, str | int]) -> None:
        text = '<div>{t("title")}</div> {name} {foo()}'
        assert resolve(text, variables) == '<div>{t("title")}</div> shot.png {foo()}'

    def test_no_attribute_access(self, variables: dict[str, str | int]) -> None:
        # Not valid call syntax and not a known function: left verbatim like any other non-macro.
        assert resolve("{os.system('x')}", variables) == "{os.system('x')}"
        with pytest.raises(MacroFunctionSyntaxError):
            resolve("{upper(name.__class__)}", variables)

    def test_too_few_arguments(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArityError) as exc_info:
            resolve("{with_ext(name)}", variables)
        assert exc_info.value.function_name == "with_ext"
        assert "2 arguments" in str(exc_info.value)

    def test_too_many_arguments(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArityError):
            resolve("{upper(name, name)}", variables)

    def test_unknown_variable_argument(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionArgumentError) as exc_info:
            resolve("{upper(missing)}", variables)
        assert exc_info.value.variable_name == "missing"

    def test_malformed_call_to_known_function(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionSyntaxError):
            resolve('{upper("unterminated)}', variables)

    def test_malformed_unknown_call_is_left_verbatim(self, variables: dict[str, str | int]) -> None:
        assert resolve("if {x(1) y} z", variables) == "if {x(1) y} z"

    def test_no_partial_resolution_on_error(self, variables: dict[str, str | int]) -> None:
        with pytest.raises(MacroFunctionError):
            resolve("{name} {add(name, 1)}", variables)


class TestExistingSyntaxUnchanged:
    def test_plain_variable(self, variables: dict[str, str | int]) -> None:
        assert resolve("{name}", variables) == "shot.png"

    def test_format_spec(self, variables: dict[str, str | int]) -> None:
        assert resolve("{count:03}", variables) == "004"

    def test_optional_missing(self, variables: dict[str, str | int]) -> None:
        assert resolve("a{missing?}b", variables) == "ab"

    def test_unknown_variable_left_verbatim(self, variables: dict[str, str | int]) -> None:
        assert resolve("{missing}", variables) == "{missing}"

    def test_prose_braces_left_verbatim(self, variables: dict[str, str | int]) -> None:
        assert resolve("body {color: red}", variables) == "body {color: red}"


class TestResolverHelpers:
    def test_references_variable_inside_call(self) -> None:
        assert VariableResolver.references_variable('{path_join(outputs, upper(name), "x")}', "name")
        assert VariableResolver.references_variable('{path_join(outputs, upper(name), "x")}', "outputs")
        assert not VariableResolver.references_variable('{path_join(outputs, "name")}', "name")

    def test_would_substitute_call(self, variables: dict[str, str | int]) -> None:
        assert VariableResolver.would_substitute("{upper(name)}", variables)

    def test_would_substitute_failing_call_does_not_raise(self, variables: dict[str, str | int]) -> None:
        assert VariableResolver.would_substitute("{upper()}", variables)

    def test_would_substitute_unknown_function_is_false(self, variables: dict[str, str | int]) -> None:
        assert not VariableResolver.would_substitute("{nope()}", variables)

    def test_contains_variable_macro_detects_call(self) -> None:
        assert VariableResolver.contains_variable_macro("{now()}")


class TestListing:
    def test_every_function_has_signature_and_description(self) -> None:
        functions = MacroFunctions.list_functions()
        names = [f.name for f in functions]
        assert names == sorted(names)
        expected = {
            "path_join",
            "ext",
            "with_ext",
            "basename",
            "dirname",
            "stem",
            "temp_dir",
            "temp_file",
            "now",
            "date",
            "random",
            "random_text",
            "join",
            "format",
            "upper",
            "lower",
            "add",
            "sub",
            "mul",
            "div",
            "round",
            "min",
            "max",
        }
        assert set(names) == expected
        for function in functions:
            assert function.signature.startswith(f"{function.name}(")
            assert function.description
