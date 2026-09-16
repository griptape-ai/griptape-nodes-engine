"""Fixture nodes for the delete-a-node-during-a-run e2e tests.

A test that deletes a node mid-run needs the run to still be mid-run when it does, and sleeping
for "long enough" makes that a race. ``GatedStreamNode`` instead parks inside ``aprocess`` until a
file appears on disk, so the test decides exactly when the node is allowed to finish: it deletes
while the node is parked, then touches the gate file.

The node also streams text while it is parked. Text appearing on a node in the editor *is* a series
of ``ProgressEvent``s, so a node that keeps computing but stops announcing chunks is
indistinguishable, on screen, from a run that died. Streaming is what makes that observable from a
test.

Kept dependency-free so the library registers cleanly in an isolated engine.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode

# Only ever reached if the test forgot to open the gate, so it just needs to be shorter than the
# suite timeout and long enough to never fire on a loaded machine.
_MAX_GATE_WAIT_SECONDS = 60
_GATE_POLL_SECONDS = 0.01


class GatedStreamNode(ControlNode):
    """Echoes its input, but only after its gate file appears.

    A ``ControlNode`` so the same fixture serves both single-node runs and control-chain runs.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to echo when nothing is connected to `linked_text`",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        # Deliberately has no PROPERTY mode: that is what makes deleting a connection into it clear
        # the value, which is the case a running node has to survive.
        self.add_parameter(
            Parameter(
                name="linked_text",
                tooltip="Text fed by a connection. Takes precedence over `text` when set.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="gate_file",
                tooltip="Path this node waits for before finishing. Empty means finish immediately.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="chunk",
                tooltip="Text streamed into `stream` on every poll while waiting. Empty means do not stream.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="The echoed text",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="stream",
                tooltip="Everything streamed while this node was waiting on its gate",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    async def aprocess(self) -> None:
        await self._wait_for_gate()

        # Read after the gate opens, not before: a connection deleted while this node was parked
        # would otherwise not be visible in the output.
        linked_text = self.get_parameter_value("linked_text") or ""
        text = self.get_parameter_value("text") or ""
        if linked_text:
            self.parameter_output_values["result"] = linked_text
        else:
            self.parameter_output_values["result"] = text

    async def _wait_for_gate(self) -> None:
        gate_path = self.get_parameter_value("gate_file") or ""
        if not gate_path:
            return

        gate_file = Path(gate_path)
        chunk = self.get_parameter_value("chunk") or ""
        deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
        while not gate_file.exists():  # noqa: ASYNC240
            if time.monotonic() > deadline:
                msg = (
                    f"Node '{self.name}' waited {_MAX_GATE_WAIT_SECONDS}s for its gate file to appear "
                    f"and it never did. The test that was supposed to open the gate did not get there."
                )
                raise TimeoutError(msg)
            if chunk:
                self.append_value_to_parameter("stream", chunk)
            await asyncio.sleep(_GATE_POLL_SECONDS)


class GatedStreamStartNode(StartNode):
    """Start Flow node, so a control chain has somewhere to begin."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text handed to the chain",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["text"] = self.get_parameter_value("text") or ""


class GatedStreamEndNode(EndNode):
    """End Flow node, so reaching the end of a control chain is observable."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="Text the chain produced",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = self.get_parameter_value("result") or ""
