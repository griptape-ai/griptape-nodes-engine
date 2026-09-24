from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.arbitrary_python_events import (
    RunArbitraryPythonStringRequest,
    RunArbitraryPythonStringResultFailure,
    RunArbitraryPythonStringResultSuccess,
)


class TestArbitraryCodeExecManager:
    def test_stdout_captured_as_output(self, engine: Engine) -> None:
        """Printed output must be returned as python_output when no variable capture is requested."""
        request = RunArbitraryPythonStringRequest(python_string="print('hello')")
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.python_output == "hello\n"

    def test_no_stdout_returns_empty_string(self, engine: Engine) -> None:
        """Code that produces no output must yield an empty string, not None or an error."""
        request = RunArbitraryPythonStringRequest(python_string="x = 1 + 1")
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.python_output == ""

    def test_ansi_codes_stripped_from_stdout(self, engine: Engine) -> None:
        """ANSI codes in printed output must be stripped before returning."""
        ansi_red = "\x1b[31m"
        ansi_reset = "\x1b[0m"
        request = RunArbitraryPythonStringRequest(python_string=f"print('{ansi_red}red{ansi_reset}')")
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.python_output == "red\n"

    def test_local_variable_capture_returns_both_stdout_and_value(self, engine: Engine) -> None:
        """When capturing a variable, stdout is still surfaced and the captured value lives in found_variable_values."""
        request = RunArbitraryPythonStringRequest(
            python_string="print('hello')\nresult = 'captured'",
            variable_names_to_capture=["result"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.python_output == "hello\n"
        assert result.found_variable_values == {"result": "captured"}
        assert result.missing_variables == []

    def test_missing_local_variable_reported_in_missing_variables(self, engine: Engine) -> None:
        """Requesting a variable that was never set must succeed with the name listed in missing_variables."""
        request = RunArbitraryPythonStringRequest(
            python_string="x = 1",
            variable_names_to_capture=["nonexistent"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.found_variable_values == {}
        assert result.missing_variables == ["nonexistent"]

    def test_partial_capture_splits_found_and_missing(self, engine: Engine) -> None:
        """When some requested names exist and others don't, found and missing must split cleanly."""
        request = RunArbitraryPythonStringRequest(
            python_string="a = 1",
            variable_names_to_capture=["a", "missing_a", "missing_b"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.found_variable_values == {"a": 1}
        assert result.missing_variables == ["missing_a", "missing_b"]

    def test_exec_exception_returns_failure(self, engine: Engine) -> None:
        """An exception raised during exec must return a failure with the error message."""
        request = RunArbitraryPythonStringRequest(python_string="raise ValueError('boom')")
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultFailure)
        assert "ERROR:" in result.python_output
        assert "boom" in result.python_output

    def test_syntax_error_returns_failure(self, engine: Engine) -> None:
        """A syntax error in the submitted code must return a failure, not raise inside the manager."""
        request = RunArbitraryPythonStringRequest(python_string="def bad(: pass")
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultFailure)
        assert "ERROR:" in result.python_output

    def test_recursive_function_works(self, engine: Engine) -> None:
        """Recursive functions defined in exec'd code must be able to call themselves."""
        code = "def fact(n): return 1 if n <= 1 else n * fact(n - 1)\nresult = fact(5)"
        request = RunArbitraryPythonStringRequest(
            python_string=code,
            variable_names_to_capture=["result"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.found_variable_values == {"result": 120}  # 5!

    def test_outer_scope_isolated(self, engine: Engine) -> None:
        """Exec'd code must not be able to access variables from the calling scope."""
        request = RunArbitraryPythonStringRequest(
            python_string=("try:\n    _ = string_buffer\n    result = False\nexcept NameError:\n    result = True"),
            variable_names_to_capture=["result"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.found_variable_values == {"result": True}

    def test_multiple_variables_captured_as_dict(self, engine: Engine) -> None:
        """Multiple variable names must return a dict mapping each name to its native value."""
        request = RunArbitraryPythonStringRequest(
            python_string="a = 1\nb = 'hello'\nc = [1, 2, 3]",
            variable_names_to_capture=["a", "b", "c"],
        )
        result = engine.handle_request(request)

        assert isinstance(result, RunArbitraryPythonStringResultSuccess)
        assert result.found_variable_values == {"a": 1, "b": "hello", "c": [1, 2, 3]}
        assert result.missing_variables == []
