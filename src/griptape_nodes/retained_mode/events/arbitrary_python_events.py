from __future__ import annotations

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.serialization.values import DisplayValue

# Eyes open about this one, yessir.
# THIS IS CONFIGURABLE BEHAVIOR. CUSTOMERS NOT WISHING TO ENABLE IT CAN DISABLE IT.
# NO FUNCTION-CRITICAL RELIANCE ON THESE EVENTS.


@dataclass
@PayloadRegistry.register
class RunArbitraryPythonStringRequest(RequestPayload):
    """Execute arbitrary Python code string.

    Use when: Development/debugging, testing code snippets, prototyping,
    educational purposes. WARNING: This is configurable behavior that can be disabled.

    Args:
        python_string: Python code string to execute
        variable_names_to_capture: Optional names of local variables in the executed code to capture and return alongside stdout.

    Results: RunArbitraryPythonStringResultSuccess (with output) | RunArbitraryPythonStringResultFailure (execution error)
    """

    python_string: str
    variable_names_to_capture: list[str] | None = None


@dataclass
@PayloadRegistry.register
class RunArbitraryPythonStringResultSuccess(ResultPayloadSuccess):
    """Python code executed successfully.

    Args:
        python_output: Stdout from the executed Python code, with ANSI escape codes stripped.
        found_variable_values: Map of requested variable names to their captured values. Empty when no capture was requested.
        missing_variables: Requested variable names that were not present in the executed code's namespace. Empty when no capture was requested or all names were found.
    """

    python_output: str
    found_variable_values: dict[str, DisplayValue] = field(default_factory=dict)
    missing_variables: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class RunArbitraryPythonStringResultFailure(ResultPayloadFailure):
    """Python code execution failed.

    Args:
        python_output: Error output from the failed Python code execution
    """

    python_output: str
