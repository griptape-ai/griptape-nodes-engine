"""Nodes exercising the behaviors a heavy library depends on when it runs in a worker.

Each node here stands in for something a real library (diffusers, advanced media) does that
the minimal fixtures did not cover:

- ``SaveMediaNode``: writes bytes and returns a URL. Proves a worker's asset URLs are served
  by the orchestrator's long-lived server rather than the worker's ephemeral one.
- ``ReadConfigNode``: reads config and a secret through requests. Proves the sanctioned
  state-access path works from inside a worker.
- ``StreamingNode``: streams progress and uses the ``AsyncResult`` yield pattern, which 24
  standard-library files rely on.
- ``ChainStartNode`` / ``ChainMiddleNode`` / ``ChainEndNode``: a three-hop chain of
  serializable values, so every hop round-trips through the orchestrator.
- ``EditorBehaviorNode``: converters, validators, and parameters added and removed from
  ``after_value_set`` -- all of which now run orchestrator-side on the real class instead of
  being lost to a schema stub.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, DataNode
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

MEDIA_BYTES = b"\x89PNG\r\n\x1a\n" + b"fixture-media-payload"


class SaveMediaNode(DataNode):
    """Writes bytes through the storage driver and outputs the resulting URL."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="url",
                type="str",
                default_value="",
                tooltip="URL of the saved media",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        # Storage is the one capability a worker keeps locally: the bytes must not cross the
        # process boundary. The URL, however, has to point at a server that outlives us.
        url = GriptapeNodes.StaticFilesManager().save_static_file(MEDIA_BYTES, f"{self.name}.png")
        self.parameter_output_values["url"] = url


class ReadConfigNode(DataNode):
    """Reads engine state the sanctioned way: a request, not a manager reference."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="config_key",
                type="str",
                default_value="workspace_directory",
                tooltip="Config key to read",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="config_value",
                type="str",
                default_value="",
                tooltip="Value the engine reported",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        key = self.get_parameter_value("config_key")
        result = GriptapeNodes.handle_request(GetConfigValueRequest(category_and_key=key))
        value = result.value if isinstance(result, GetConfigValueResultSuccess) else ""
        self.parameter_output_values["config_value"] = str(value)


class StreamingNode(DataNode):
    """Streams progress mid-execution and yields work the framework runs off-thread."""

    CHUNKS = ("alpha", "beta", "gamma")

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="stream",
                type="str",
                default_value="",
                tooltip="Accumulated streamed output",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> AsyncResult:
        def work() -> str:
            return "".join(self.CHUNKS)

        for chunk in self.CHUNKS:
            self.append_value_to_parameter("stream", chunk)
        # The yield-a-callable pattern: the framework runs this off the event loop and sends
        # the result back into the generator.
        total = yield work
        self.parameter_output_values["stream"] = total


class ChainStartNode(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="out",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["out"] = "start"


class ChainMiddleNode(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="in_value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="out",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["out"] = f"{self.get_parameter_value('in_value')}->middle"


class ChainEndNode(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="in_value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="final",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["final"] = f"{self.get_parameter_value('in_value')}->end"


def _shout(value: Any) -> Any:
    """Converter: proves converters run where the node object is real."""
    if isinstance(value, str):
        return value.upper()
    return value


def _reject_forbidden(parameter: Parameter, value: Any) -> None:  # noqa: ARG001 (validator signature)
    """Validator: proves validators run where the node object is real."""
    if value == "FORBIDDEN":
        msg = "value 'forbidden' is not allowed"
        raise ValueError(msg)


class EditorBehaviorNode(DataNode):
    """Carries converters, validators, and dynamic parameters driven by value changes.

    A schema stub would drop all of this: the converter and validator would not travel, and
    the dynamic parameter would never appear. With a real class on the orchestrator they all
    work at edit time, which is what the dependency split buys.
    """

    DYNAMIC_PARAMETER = "dynamic_extra"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="mode",
                type="str",
                default_value="plain",
                tooltip="Set to 'expand' to grow a dynamic parameter",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                converters=[_shout],
                validators=[_reject_forbidden],
            )
        )
        self.add_parameter(
            Parameter(
                name="observed_mode",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name != "mode":
            return
        wants_extra = value == "EXPAND"
        has_extra = self.get_parameter_by_name(self.DYNAMIC_PARAMETER) is not None
        if wants_extra and not has_extra:
            GriptapeNodes.handle_request(
                AddParameterToNodeRequest(
                    node_name=self.name,
                    parameter_name=self.DYNAMIC_PARAMETER,
                    type="str",
                    default_value="grown",
                    tooltip="Added because mode is expand",
                    mode_allowed_input=True,
                    mode_allowed_property=True,
                    mode_allowed_output=False,
                )
            )
        elif not wants_extra and has_extra:
            GriptapeNodes.handle_request(
                RemoveParameterFromNodeRequest(node_name=self.name, parameter_name=self.DYNAMIC_PARAMETER)
            )

    def process(self) -> None:
        self.parameter_output_values["observed_mode"] = self.get_parameter_value("mode")
