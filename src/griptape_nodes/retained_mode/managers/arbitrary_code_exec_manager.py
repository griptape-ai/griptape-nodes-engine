from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.arbitrary_python_events import (
    RunArbitraryPythonStringRequest,
    RunArbitraryPythonStringResultFailure,
    RunArbitraryPythonStringResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape sequences (e.g. terminal color codes) from the given string.

    Args:
        text: A string that may contain ANSI escape codes.

    Returns:
        A cleaned string with all ANSI escape sequences removed.
    """
    return ANSI_ESCAPE_RE.sub("", text)


class ArbitraryCodeExecManager:
    def __init__(self, event_manager: EventManager) -> None:
        event_manager.register_request_handlers(self)

    @handles(RunArbitraryPythonStringRequest)
    def on_run_arbitrary_python_string_request(self, request: RunArbitraryPythonStringRequest) -> ResultPayload:

        try:
            string_buffer = io.StringIO()
            with redirect_stdout(string_buffer):
                # Use a shared namespace for both globals and locals in exec() to make some behavior possible and more intuitive:
                #
                # 1. RECURSION: Without this namespace, recursive functions defined inside exec() fail with
                #    "NameError: name 'function_name' is not defined" when they try to call themselves.
                #    Why? When exec() runs with default parameters, functions defined in the exec'd code
                #    exist in this method's local scope. But inside the exec'd functions, Python looks in the program's
                #    global scope (outside this method) and the function's own local scope - neither of which
                #    contains the recursive function definition. By passing the same dict as both globals and locals,
                #    any function defined in exec'd code becomes visible in what exec'd code sees as
                #    "global" scope, allowing recursive calls to find the function definition.
                #
                # 2. ISOLATION: An isolated namespace prevents exec'd code from accessing or modifying
                #    variables in the outer program scope, protecting read/write access to sensitive engine data.
                # For the PR that implements this behavior alongside an Execute Python and List Files node, see https://github.com/griptape-ai/griptape-nodes/pull/2087

                namespace = {"__builtins__": __builtins__}
                exec(  # noqa: S102
                    request.python_string, namespace, namespace
                )

            python_output = strip_ansi_codes(string_buffer.getvalue())
            found_variable_values: dict[str, Any] = {}
            missing_variables: list[str] = []
            if request.variable_names_to_capture is not None:
                for name in request.variable_names_to_capture:
                    if name in namespace:
                        found_variable_values[name] = namespace[name]
                    else:
                        missing_variables.append(name)
            result = RunArbitraryPythonStringResultSuccess(
                python_output=python_output,
                found_variable_values=found_variable_values,
                missing_variables=missing_variables,
                result_details="Successfully executed Python string",
            )
        except Exception as e:
            error_output = f"ERROR: {e}"
            result = RunArbitraryPythonStringResultFailure(python_output=error_output, result_details=error_output)

        return result
