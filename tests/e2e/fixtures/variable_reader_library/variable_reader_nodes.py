"""Fixture nodes for the variable change re-run tests.

``VariableReader`` reads its ``text`` parameter, where a ``{name}`` token is substituted, the moment
it starts, then waits for an optional gate file before publishing. The gate is what lets a test
change a variable while the reader is still running, so the test decides the interleaving instead
of racing it. Every node counts its runs in ``run_count``, so a test can tell "reused" from "ran
again" without trusting resolution state.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

_MAX_GATE_WAIT_SECONDS = 60
_GATE_POLL_SECONDS = 0.01


def _run_count_parameter() -> Parameter:
    return Parameter(
        name="run_count",
        type="int",
        default_value=0,
        tooltip="How many times this node has run",
        allowed_modes={ParameterMode.OUTPUT},
    )


class VariableReader(DataNode):
    """Publishes `text` as it read it when it started, optionally after waiting on `gate_file`."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="Text that may hold {name} tokens",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="gate_file",
                type="str",
                default_value="",
                tooltip="Path to wait for before finishing. Empty means finish immediately.",
                allowed_modes={ParameterMode.PROPERTY},
                allow_variable_substitution=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="The text as read at the start of the run",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(_run_count_parameter())
        self._runs = 0

    async def aprocess(self) -> None:
        self._runs += 1
        text = self.get_parameter_value("text") or ""
        await self._wait_for_gate()
        self.parameter_output_values["result"] = text
        self.parameter_output_values["run_count"] = self._runs

    async def _wait_for_gate(self) -> None:
        gate_path = self.get_parameter_value("gate_file") or ""
        if not gate_path:
            return
        gate_file = Path(gate_path)
        deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
        while not gate_file.exists():  # noqa: ASYNC240
            if time.monotonic() > deadline:
                msg = f"Node '{self.name}' waited {_MAX_GATE_WAIT_SECONDS}s for its gate file and it never appeared."
                raise TimeoutError(msg)
            await asyncio.sleep(_GATE_POLL_SECONDS)


class Consumer(DataNode):
    """Copies `value` to `result`."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="Input",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="Copy of the input",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(_run_count_parameter())
        self._runs = 0

    def process(self) -> None:
        self._runs += 1
        self.parameter_output_values["result"] = self.get_parameter_value("value")
        self.parameter_output_values["run_count"] = self._runs
