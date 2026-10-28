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
- ``DerivedStructureNode``: parameter structure derived from parameter values. Its value hook
  creates and removes a parameter as a pure function of ``shape``, on the node being edited; a
  worker's copy receives that structure from the orchestrator instead of re-running the hook.
- ``WriteBytesNode``: writes binary through ``File``, the ordinary way a node emits a file.
  Reads it back and reports whether the bytes survived, so a routing change that sends file
  I/O across the process boundary fails a test instead of silently writing mojibake.
- ``WriteProjectFileNode``: saves through ``ProjectFileDestination``, which is how a node emits
  a user-visible output. Four project/filesystem requests compose into one save, and the macro
  string it reports is what keeps a saved workflow portable.
- ``StreamingNode``: streams progress and uses the ``AsyncResult`` yield pattern, which 24
  standard-library files rely on.
- ``ChainStartNode`` / ``ChainMiddleNode`` / ``ChainEndNode``: a three-hop chain of
  serializable values, so every hop round-trips through the orchestrator.
- ``EditorBehaviorNode``: converters, validators, and parameters added and removed from
  ``after_value_set`` -- all of which now run orchestrator-side on the real class instead of
  being lost to a schema stub.
- ``ReportsHydrationNode``: reports whether its value hook ran on the copy that runs it, and
  whether its ``process`` ran inside the rebuild a worker does before running a node.
- ``ProcessInfoNode``: reports the process that ran it and that process's parent, so a test
  driving real engine processes can tell which process executed the node.
- ``EditsTheGraphWhileLoadingNode``: sends requests that edit other nodes and their connections while
  its value is applied on load (strict-mode rule ``graph-access-while-loading``). Nothing it sends
  while a worker loads its copy may reach the orchestrator's graph.
- ``ReadsConnectionsWhileLoadingNode``: only asks about its own connections while loading, the
  harmless form of the same mistake, so the rule can be watched in-process too.
- ``WaitsUntilReleasedNode``: runs until a test releases it, so another node can load while it runs,
  and a run can be cancelled mid-way.
- ``FailsInProcessNode`` / ``RefusesToRunNode``: a run that raises, and one refused by
  ``validate_in_execution_environment``.
- ``RecordsDeletionNode``: records each ``after_node_deleted`` call, which discarding a worker's copy
  must not make.
- ``ListSourceNode`` / ``ReadsABoundedListNode``: a list output feeding a ``ParameterList`` that holds
  at most two rows, so a connection can deliver more items than rows could.
- ``ReadsAManagerNode``: reads config through a manager while running (strict-mode rule
  ``state-read-bypassing-requests``).
- ``SetsAnAttributeInItsHookNode``: sets attributes from ``after_value_set`` (strict-mode rule
  ``state-outside-parameters``), one replaced, one declared in ``attributes_outside_parameters``, and
  one changed in place.
- ``PlainGroupNode``: a node group, which runs where its workflow is open and is never copied to a
  worker.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import AsyncResult, DataNode
from griptape_nodes.files.file import File
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    import asyncio

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


class ReportsWithSetterNode(DataNode):
    """Reports its results through `set_parameter_value` rather than `parameter_output_values`.

    Both spellings are common in libraries. The three parameters cover the mode combinations a library
    actually declares: OUTPUT alone, OUTPUT alongside PROPERTY so the value stays on display, and the
    default, which is every mode and is what most parameters in a library are. All three publish, so a
    value set on any of them while the node runs is a result and has to travel back from a worker.
    `scratch` has no OUTPUT, so it has no port to publish on and stays behind.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="status",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="on_display",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(Parameter(name="defaulted", type="str", default_value="", tooltip=""))
        self.add_parameter(
            Parameter(
                name="scratch",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.set_parameter_value("status", "reported")
        self.set_parameter_value("on_display", "shown")
        self.set_parameter_value("defaulted", "defaulted-result")
        self.set_parameter_value("scratch", "local only")
        # Read back through the same API the value was set with.
        self.set_parameter_value("status", self.get_parameter_value("status") + "-readback")


class GrowsAnOutputListNode(DataNode):
    """Grows an output-only `ParameterList` while running, as Split Video does.

    The children are set through `set_parameter_value`; the list itself is rebuilt from them. Only the
    rebuilt list is a result, so only it travels back from a worker.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.clips = ParameterList(
            name="clips",
            type="str",
            tooltip="",
            allowed_modes={ParameterMode.OUTPUT},
        )
        self.add_parameter(self.clips)

    def process(self) -> None:
        for item in ("clip0", "clip1"):
            child = self.clips.add_child_parameter()
            self.set_parameter_value(child.name, item)


class ReadsAnInputListNode(DataNode):
    """Echoes an input `ParameterList`, the shape of a node reading `reference_images`."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                type="str",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(Parameter(name="echo", type="list", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        self.parameter_output_values["echo"] = self.get_parameter_list_value("items")


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
        # that is executing. A worker's copy has it when the orchestrator's node had it as the run
        # was sent, since the copy gets the node's structure rather than re-running the hook.
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


class ProcessInfoNode(DataNode):
    """Outputs the ids of the process that executed it and of that process's parent."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="pid",
                type="int",
                default_value=0,
                tooltip="Id of the process that executed this node",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="parent_pid",
                type="int",
                default_value=0,
                tooltip="Id of that process's parent",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["pid"] = os.getpid()
        self.parameter_output_values["parent_pid"] = os.getppid()


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

    The diffusers VAE decoder's dynamic-structure pattern, minus the pipeline: the value hook
    mutates the node directly as it is edited. A worker's copy runs no value hooks, so it gets
    the derived parameters from the orchestrator's node, the way a saved workflow reloads them.
    Contrast EditorBehaviorNode, whose hook goes through a request.
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


class ReportsHydrationNode(DataNode):
    """Reports whether its value hook ran on the copy that runs it, and whether process() ran inside the rebuild.

    A worker loads its copy without running value hooks, and answers requests locally only while it
    rebuilds, so the rebuild has to end before process() runs.
    """

    # Only this node's own report reads it, which is the point.
    attributes_outside_parameters = frozenset({"_hook_ran"})

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self._hook_ran = False
        self.add_parameter(
            Parameter(
                name="trigger",
                type="str",
                default_value="",
                tooltip="Any value runs the hook",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        for output_name in ("hook_ran", "process_saw_rebuild"):
            self.add_parameter(
                Parameter(
                    name=output_name,
                    type="bool",
                    default_value=False,
                    tooltip="",
                    allowed_modes={ParameterMode.OUTPUT},
                )
            )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:  # noqa: ARG002
        if parameter.name == "trigger":
            self._hook_ran = True

    def process(self) -> None:
        self.parameter_output_values["hook_ran"] = self._hook_ran
        self.parameter_output_values["process_saw_rebuild"] = (
            current_engine().event_manager.rebuilding_node_name() is not None
        )


class WriteProjectFileNode(DataNode):
    """Saves through the project system, the way a node emits a user-visible output.

    ``ProjectFileDestination`` composes four requests -- the situation lookup, the macro
    resolution, the write, and the map back to macro form -- and every one of them is answered
    locally in a worker. None of them touches a manager accessor, which is what makes this legal
    where ``GriptapeNodes.OSManager()`` is refused.

    Reports the macro string rather than the absolute path: the mapping back to ``{outputs}/...``
    is the part that makes a saved workflow portable, and it is the step that would quietly
    degrade to an absolute path if the project reads stopped being answered.
    """

    PAYLOAD = b"\x89PNG\r\n\x1a\n\x00\xff\xfe project-payload"

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
                name="saved_as",
                type="str",
                default_value="",
                tooltip="The path the destination handed back, macro form when mapping succeeded",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        destination = ProjectFileDestination.from_situation(
            f"{self.name}-payload.bin",
            BuiltInSituation.SAVE_NODE_OUTPUT,
            node_name=self.name,
        )
        saved = destination.write_bytes(self.PAYLOAD)
        self.parameter_output_values["bytes_survived"] = saved.read_bytes() == self.PAYLOAD
        # A mapped path comes back as a macro string, which File wraps into a MacroPath. Report
        # the template: str() on the MacroPath gives its repr, which would satisfy a substring
        # assertion without the mapping having happened.
        location = saved._file_path
        if isinstance(location, MacroPath):
            self.parameter_output_values["saved_as"] = location.parsed_macro.template
        else:
            self.parameter_output_values["saved_as"] = str(location)


class EditsTheGraphWhileLoadingNode(DataNode):
    """Edits other nodes and their connections while its ``trigger`` value is applied on load.

    The mistake ``graph-access-while-loading`` names, in its most harmful form: run on the
    orchestrator's graph, these would remove a parameter and rewire the nodes around it. The names
    are fixed so a test can build the graph they aim at.
    """

    TARGET_NODE = "Graph Edit Target"
    UPSTREAM_NODE = "Graph Edit Upstream"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="trigger",
                type="str",
                default_value="",
                tooltip="Any value set on load sends the edits",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def set_parameter_value(
        self,
        param_name: str,
        value: Any,
        *,
        initial_setup: bool = False,
        emit_change: bool = True,
        skip_before_value_set: bool = False,
    ) -> None:
        if initial_setup and param_name == "trigger":
            GriptapeNodes.handle_request(
                RemoveParameterFromNodeRequest(node_name=self.TARGET_NODE, parameter_name="in_value")
            )
            GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=self.UPSTREAM_NODE,
                    source_parameter_name="out",
                    target_node_name=self.TARGET_NODE,
                    target_parameter_name="in_value",
                )
            )
            GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=self.UPSTREAM_NODE,
                    source_parameter_name="out",
                    target_node_name=self.name,
                    target_parameter_name="trigger",
                )
            )
        super().set_parameter_value(
            param_name,
            value,
            initial_setup=initial_setup,
            emit_change=emit_change,
            skip_before_value_set=skip_before_value_set,
        )

    def process(self) -> None:
        pass


class ReadsConnectionsWhileLoadingNode(DataNode):
    """Asks about its own connections while its ``trigger`` value is applied on load.

    Harmless anywhere, so ``graph-access-while-loading`` can be watched in-process as well as in a worker.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="trigger",
                type="str",
                default_value="",
                tooltip="Any value set on load sends the question",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def set_parameter_value(
        self,
        param_name: str,
        value: Any,
        *,
        initial_setup: bool = False,
        emit_change: bool = True,
        skip_before_value_set: bool = False,
    ) -> None:
        if initial_setup and param_name == "trigger":
            GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=self.name))
        super().set_parameter_value(
            param_name,
            value,
            initial_setup=initial_setup,
            emit_change=emit_change,
            skip_before_value_set=skip_before_value_set,
        )

    def process(self) -> None:
        pass


class WaitsUntilReleasedNode(DataNode):
    """Runs until a test sets ``release``, recording in ``running`` the names of the nodes that started.

    A test sets ``release`` to a fresh ``asyncio.Event`` before running one, on the loop that runs it.
    """

    running: ClassVar[set[str]] = set()
    release: ClassVar[asyncio.Event | None] = None

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(name="done", type="bool", default_value=False, tooltip="", allowed_modes={ParameterMode.OUTPUT})
        )

    async def aprocess(self) -> None:
        release = type(self).release
        if release is None:
            msg = "Attempted to run WaitsUntilReleasedNode. Failed because no release event was set."
            raise RuntimeError(msg)
        type(self).running.add(self.name)
        await release.wait()
        self.parameter_output_values["done"] = True

    def process(self) -> None:
        pass


class FailsInProcessNode(DataNode):
    """Raises while running."""

    def process(self) -> None:
        msg = "this node always fails"
        raise RuntimeError(msg)


class RefusesToRunNode(DataNode):
    """Refused by ``validate_in_execution_environment``, so it never runs."""

    def validate_in_execution_environment(self) -> list[Exception] | None:
        return [ValueError("this node never runs here")]

    def process(self) -> None:
        pass


class RecordsDeletionNode(DataNode):
    """Records in ``deleted`` the name of each node of its type whose ``after_node_deleted`` ran."""

    deleted: ClassVar[list[str]] = []

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(name="ran", type="bool", default_value=False, tooltip="", allowed_modes={ParameterMode.OUTPUT})
        )

    def after_node_deleted(self) -> None:
        type(self).deleted.append(self.name)

    def process(self) -> None:
        self.parameter_output_values["ran"] = True


class ListSourceNode(DataNode):
    """Outputs a list of three strings, more than ``ReadsABoundedListNode`` has rows for."""

    ITEMS = ("first", "second", "third")

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="items",
                type="list[str]",
                output_type="list[str]",
                default_value=[],
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["items"] = list(self.ITEMS)


class ReadsABoundedListNode(DataNode):
    """Echoes an input ``ParameterList`` that holds at most two rows."""

    MAX_ITEMS = 2

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                type="str",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                max_items=self.MAX_ITEMS,
            )
        )
        self.add_parameter(Parameter(name="echo", type="list", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        self.parameter_output_values["echo"] = self.get_parameter_list_value("items")


class ReadsAManagerNode(DataNode):
    """Reads config through a manager while running, instead of with a request."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="config_value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        value = GriptapeNodes.ConfigManager().get_config_value("workspace_directory")
        self.parameter_output_values["config_value"] = str(value)


class SetsAnAttributeInItsHookNode(DataNode):
    """Sets attributes from ``after_value_set`` on ``note``, chosen by ``mode``.

    - ``replace``: sets ``stash``, which nothing declares.
    - ``declared``: sets ``declared_stash``, listed in ``attributes_outside_parameters``.
    - ``in_place``: appends to ``seen``, a list made in ``__init__``.

    Running sets ``note`` itself, so the hook runs during execution too.
    """

    attributes_outside_parameters = frozenset({"declared_stash"})

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.seen: list[Any] = []
        self.add_parameter(
            Parameter(
                name="mode",
                type="str",
                default_value="replace",
                tooltip="Which attribute the hook sets",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="note",
                type="str",
                default_value="",
                tooltip="Setting it runs the hook",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name != "note":
            return
        mode = self.get_parameter_value("mode")
        if mode == "replace":
            self.stash = value
        elif mode == "declared":
            self.declared_stash = value
        elif mode == "in_place":
            self.seen.append(value)

    def process(self) -> None:
        self.set_parameter_value("note", "set while running")


class PlainGroupNode(BaseNodeGroup):
    """A node group with no subflow of its own."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        pass
