"""Nodes exercising the behaviors a heavy library depends on when it runs in a worker.

Each node here stands in for something a real library (diffusers, advanced media) does that
the minimal fixtures did not cover:

- ``SaveMediaNode``: writes bytes and returns a URL. Proves a worker's asset URLs are served
  by the orchestrator's long-lived server rather than the worker's ephemeral one.
- ``ReadConfigNode`` / ``ReadSecretNode``: read config and a secret through requests. Prove
  the sanctioned state-access path works from inside a worker, where the manager path is
  refused and a local read would answer from the wrong process.
- ``UnshippableOutputNode``: outputs a value its author declared unserializable, which a
  worker must refuse to ship rather than drop on the wire.
- ``DerivedStructureNode``: the authoring contract's reference shape -- parameter structure
  derives from parameter values. Its value hook creates and removes a parameter as a pure
  function of ``shape``, so any fresh copy of the node converges on the right structure the
  moment its values hydrate, in any order.
- ``WriteBytesNode``: writes binary through ``File``, the ordinary way a node emits a file.
  Reads it back and reports whether the bytes survived, so a routing change that sends file
  I/O across the process boundary fails a test instead of silently writing mojibake.
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
from griptape_nodes.files.file import File
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
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
        self.add_parameter(
            Parameter(
                name="dynamic_visible_in_process",
                type="bool",
                default_value=False,
                tooltip="Whether the parameter after_value_set added is visible to process()",
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
        # Whether the parameter this node's own after_value_set added is present on the instance
        # that is executing. In a worker the add is a REQUEST, and requests are answered by the
        # orchestrator -- so the parameter can land on the orchestrator's node while this copy,
        # the one running process(), never sees it.
        self.parameter_output_values["dynamic_visible_in_process"] = (
            self.get_parameter_by_name(self.DYNAMIC_PARAMETER) is not None
        )


class ReadSecretNode(DataNode):
    """Reads a secret the sanctioned way: a request, not a manager reference.

    Secrets are the case that matters most in practice -- almost every real node needs an API
    key -- and the one where reading the worker's own copy is most likely to differ from the
    orchestrator's, because a worker's environment is frozen when it spawns.
    """

    # The NAME of a secret, not a secret.
    SECRET_NAME = "GTN_WORKER_FIXTURE_SECRET"  # noqa: S105

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="secret_value",
                type="str",
                default_value="",
                tooltip="Value the engine reported for the fixture secret",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        result = GriptapeNodes.handle_request(
            GetSecretValueRequest(key=self.SECRET_NAME, should_error_on_not_found=False)
        )
        value = result.value if isinstance(result, GetSecretValueResultSuccess) else None
        self.parameter_output_values["secret_value"] = value or ""


class UnshippableOutputNode(DataNode):
    """Produces a value that cannot cross a process boundary.

    ``serializable=False`` is the author saying "this is unreasonable to move" -- a live
    tensor, an open pipeline handle. Executing in a worker, that output has nowhere to go, and
    the engine must say so instead of silently dropping it and leaving a hole in the graph.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="live_handle",
                type="Any",
                default_value=None,
                tooltip="Stands in for a tensor or an open pipeline",
                allowed_modes={ParameterMode.OUTPUT},
                serializable=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="summary",
                type="str",
                default_value="",
                tooltip="A serializable descriptor, which is what a library should output instead",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        # An object with no meaningful serialized form, which is the whole point.
        self.parameter_output_values["live_handle"] = object()
        self.parameter_output_values["summary"] = "handle-1"


class WriteBytesNode(DataNode):
    """Writes binary through ``File`` and reports whether it survived the round trip.

    ``File.write_bytes`` goes through ``WriteFileRequest``, so this is the path a node takes to
    emit a file. It exists because that request was briefly forwarded from workers, and the wire
    form turned bytes into a base64 string that came back as ``str`` -- every write landing
    corrupted, with no error anywhere. Asserting on the bytes read back is the only thing that
    catches it.
    """

    PAYLOAD = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary-payload"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="bytes_survived",
                type="bool",
                default_value=False,
                tooltip="True when the bytes read back match the bytes written",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="byte_count",
                type="int",
                default_value=0,
                tooltip="Length of what was read back",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        target = File(f"{self.name}-payload.bin")
        target.write_bytes(self.PAYLOAD)
        read_back = target.read_bytes()
        self.parameter_output_values["bytes_survived"] = read_back == self.PAYLOAD
        self.parameter_output_values["byte_count"] = len(read_back)


class DerivedStructureNode(DataNode):
    """Parameter structure as a deterministic function of parameter values.

    This is the sanctioned dynamic-structure pattern (the diffusers VAE decoder's, minus the
    pipeline): the value hook mutates the LOCAL node directly, so the derivation runs wherever
    the values land -- on the orchestrator at edit time, and again on a fresh worker copy as
    hydration applies the same values. Nothing needs to sync structure, because structure is
    recomputed. Contrast EditorBehaviorNode, whose hook goes through a request and therefore
    mutates only the authoritative node.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="shape",
                type="str",
                default_value="plain",
                tooltip="Set to 'expanded' to derive the extra parameter",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="echo",
                type="str",
                default_value="",
                tooltip="Echoes the derived parameter's hydrated value",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="deep_echo",
                type="str",
                default_value="",
                tooltip="Echoes the second-level derived parameter's hydrated value",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name == "shape":
            wants_derived = value == "expanded"
            has_derived = self.get_parameter_by_name("derived_in") is not None
            if wants_derived and not has_derived:
                self.add_parameter(
                    Parameter(
                        name="derived_in",
                        type="str",
                        default_value="",
                        tooltip="Exists exactly when shape is 'expanded'",
                        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                    )
                )
            elif not wants_derived and has_derived:
                self.remove_parameter_element_by_name("derived_in")
            return
        if parameter.name == "derived_in":
            # Second derivation level: the chain shape real dynamic UIs take (provider creates
            # model, model creates options). Exists exactly when derived_in is 'deeper'.
            wants_deep = value == "deeper"
            has_deep = self.get_parameter_by_name("derived_deep") is not None
            if wants_deep and not has_deep:
                self.add_parameter(
                    Parameter(
                        name="derived_deep",
                        type="str",
                        default_value="",
                        tooltip="Exists exactly when derived_in is 'deeper'",
                        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                    )
                )
            elif not wants_deep and has_deep:
                self.remove_parameter_element_by_name("derived_deep")

    def process(self) -> None:
        derived = self.get_parameter_value("derived_in") if self.get_parameter_by_name("derived_in") else ""
        self.parameter_output_values["echo"] = derived
        deep = self.get_parameter_value("derived_deep") if self.get_parameter_by_name("derived_deep") else ""
        self.parameter_output_values["deep_echo"] = deep
