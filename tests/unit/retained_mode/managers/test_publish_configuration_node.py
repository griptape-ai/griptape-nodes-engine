"""A publish target's Configuration node: the engine creates and reuses it, and a run never executes it.

A library ships one ``ConfigurationNode`` subclass per publish target and names it when it registers
its publisher. The engine finds that node in the workflow, hands its values to the publisher, writes
back what the publisher returns, and creates the node on the first publish.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ConfigurationNode, ControlNode, EndNode, StartNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DuplicateSelectedNodesRequest,
    DuplicateSelectedNodesResultFailure,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultFailure,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    GetPublishOptionsRequest,
    GetPublishOptionsResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.library_manager import RegisteredEventHandler
from griptape_nodes.retained_mode.managers.workflow.shape import extract_workflow_shape

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY_NAME = "publish-configuration-node-test-library"
_FLOW = "canvas"


class _Start(StartNode):
    def process(self) -> None:
        pass


class _End(EndNode):
    pass


class _TargetConfiguration(ConfigurationNode):
    """A target with one remembered setting and one value its runtime supplies."""

    process_calls: ClassVar[int] = 0

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="setting",
                tooltip="A remembered publish setting",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="runtime_value",
                tooltip="A value the target supplies when the published workflow runs",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        type(self).process_calls += 1


class _Consumer(ControlNode):
    """Records the value it was handed each time it runs."""

    seen: ClassVar[list[Any]] = []

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value to record",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT},
            )
        )

    def process(self) -> None:
        type(self).seen.append(self.get_parameter_value("value"))


@pytest.fixture
def workflow(engine: Engine, tmp_path: Path) -> Iterator[WorkflowRuntime]:
    """An open, saved workflow with Start -> Consumer -> End in its top-level flow."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    LibraryRegistry._clear()
    _TargetConfiguration.process_calls = 0
    _Consumer.seen = []
    library = LibraryRegistry.generate_new_library(
        LibrarySchema(
            name=_LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
    )
    for node_class, display_name in (
        (_Start, "Start"),
        (_End, "End"),
        (_Consumer, "Consumer"),
        (_TargetConfiguration, "Target Configuration"),
    ):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="d", display_name=display_name)
        )

    path = tmp_path.resolve() / "published.py"
    path.write_text("# a saved workflow\n", encoding="utf-8")
    saved = engine.workflow_registry.register_file(
        path,
        WorkflowMetadata(
            name=path.stem,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[],
        ),
    )
    opened = engine.open_workflow(saved.workflow_id)
    flow = _send(engine, CreateFlowRequest(parent_flow_name=None, flow_name=_FLOW), opened)
    assert isinstance(flow, CreateFlowResultSuccess), flow.result_details
    for node_type, node_name in (("_Start", "Start"), ("_Consumer", "Consumer"), ("_End", "End")):
        _create(engine, opened, node_type, node_name)
    _connect(engine, opened, ("Start", "exec_out"), ("Consumer", "exec_in"))
    _connect(engine, opened, ("Consumer", "exec_out"), ("End", "exec_in"))
    try:
        yield opened
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _send(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime) -> ResultPayload:
    return asyncio.run(
        engine.event_manager.ahandle_request(
            request, result_context=ResultContext(open_workflow_id=workflow.workflow_id)
        )
    ).result


def _create(engine: Engine, workflow: WorkflowRuntime, node_type: str, node_name: str) -> None:
    created = _send(
        engine,
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=_LIBRARY_NAME,
            node_name=node_name,
            override_parent_flow_name=_FLOW,
        ),
        workflow,
    )
    assert isinstance(created, CreateNodeResultSuccess), created.result_details


def _connect(engine: Engine, workflow: WorkflowRuntime, source: tuple[str, str], target: tuple[str, str]) -> None:
    connected = _send(
        engine,
        CreateConnectionRequest(
            source_node_name=source[0],
            source_parameter_name=source[1],
            target_node_name=target[0],
            target_parameter_name=target[1],
        ),
        workflow,
    )
    assert isinstance(connected, CreateConnectionResultSuccess), connected.result_details


def _set(engine: Engine, workflow: WorkflowRuntime, node: str, parameter: str, value: Any) -> None:
    result = _send(engine, SetParameterValueRequest(node_name=node, parameter_name=parameter, value=value), workflow)
    assert isinstance(result, SetParameterValueResultSuccess), result.result_details


def _run(engine: Engine, workflow: WorkflowRuntime) -> None:
    result = _send(engine, StartFlowRequest(flow_name=_FLOW, flow_node_name=None), workflow)
    assert isinstance(result, StartFlowResultSuccess), result.result_details


def _configuration_nodes(engine: Engine, workflow: WorkflowRuntime) -> list[ConfigurationNode]:
    nodes = engine.flow_manager.get_flow_by_name_in(workflow, _FLOW).nodes
    return [node for node in nodes.values() if isinstance(node, ConfigurationNode)]


def _publisher(
    handler: Any, get_publish_options: Any = None, *, configuration_node_type: str | None = "_TargetConfiguration"
) -> RegisteredEventHandler:
    return RegisteredEventHandler(
        handler=handler,
        library_data=MagicMock(),
        event_data=PublishWorkflowRegisteredEventData(
            start_flow_node_type="_Start",
            start_flow_node_library_name=_LIBRARY_NAME,
            end_flow_node_type="_End",
            end_flow_node_library_name=_LIBRARY_NAME,
            get_publish_options=get_publish_options,
            configuration_node_type=configuration_node_type,
            configuration_node_library_name=_LIBRARY_NAME,
        ),
    )


class _Publisher:
    """Records the configuration each publish is handed and returns the next one to remember."""

    def __init__(self, *returned: dict[str, Any] | None) -> None:
        self.returned = list(returned)
        self.received: list[dict[str, Any] | None] = []

    def __call__(self, request: PublishWorkflowRequest) -> PublishWorkflowResultSuccess:
        self.received.append(request.configuration)
        return PublishWorkflowResultSuccess(
            published_workflow_file_path="",
            skip_published_workflow_registration=True,
            configuration=self.returned.pop(0),
            result_details="published",
        )


class _Saves:
    """Stands in for saving the open workflow, recording which nodes each save would have written."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.configuration_nodes_at_save: list[list[str]] = []

    async def save(self, workflow: WorkflowRuntime, _request: SaveWorkflowRequest, **_: Any) -> Any:
        self.configuration_nodes_at_save.append([node.name for node in _configuration_nodes(self.engine, workflow)])
        return SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="saved")


def _publish(
    engine: Engine, workflow: WorkflowRuntime, handler: RegisteredEventHandler, saves: _Saves
) -> ResultPayload:
    name = engine.workflow_registry.get(workflow.workflow_id).name
    with (
        patch.object(engine.library_manager, "get_registered_event_handlers", return_value={"lib": handler}),
        patch.object(engine.workflow_manager.saver, "save_open_workflow", side_effect=saves.save),
    ):
        return _send(engine, PublishWorkflowRequest(workflow_name=name, publisher_name="lib"), workflow)


class TestARunNeverExecutesAConfigurationNode:
    def test_an_unconnected_configuration_node_keeps_its_values_and_never_runs(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        _create(engine, workflow, "_TargetConfiguration", "Target Configuration")
        _set(engine, workflow, "Target Configuration", "setting", "remembered")

        _run(engine, workflow)
        _run(engine, workflow)

        configuration = engine.node_manager.get_node_by_name("Target Configuration")
        assert _TargetConfiguration.process_calls == 0
        assert configuration.get_parameter_value("setting") == "remembered"
        assert _Consumer.seen == ["", ""]

    def test_a_runtime_value_wired_into_the_flow_reaches_the_node_it_feeds(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        _create(engine, workflow, "_TargetConfiguration", "Target Configuration")
        _connect(engine, workflow, ("Target Configuration", "runtime_value"), ("Consumer", "value"))
        # What a target's entry point does before the published workflow starts.
        configuration = engine.node_manager.get_node_by_name("Target Configuration")
        configuration.parameter_output_values["runtime_value"] = "from the target"

        _run(engine, workflow)
        _run(engine, workflow)

        assert _TargetConfiguration.process_calls == 0
        assert _Consumer.seen == ["from the target", "from the target"]
        assert configuration.parameter_output_values["runtime_value"] == "from the target"

    def test_a_configuration_node_is_not_part_of_the_published_inputs_or_outputs(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        _create(engine, workflow, "_TargetConfiguration", "Target Configuration")

        shape = extract_workflow_shape(engine.flow_manager, workflow, "published")

        assert set(shape["input"]) == {"Start"}
        assert set(shape["output"]) == {"End"}


class TestPublishingRemembersTheTargetsSettings:
    def test_the_first_publish_adds_the_configuration_node_with_the_returned_values_and_saves_it(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        publisher = _Publisher({"setting": "first"})
        saves = _Saves(engine)

        result = _publish(engine, workflow, _publisher(publisher), saves)

        assert isinstance(result, PublishWorkflowResultSuccess), result.result_details
        assert publisher.received == [None]
        nodes = _configuration_nodes(engine, workflow)
        assert [node.name for node in nodes] == ["Target Configuration"]
        assert nodes[0].get_parameter_value("setting") == "first"
        # Saved before publishing, then again once the node holds what the publisher returned.
        assert saves.configuration_nodes_at_save == [[], ["Target Configuration"]]

    def test_the_next_publish_is_handed_the_remembered_values_and_reuses_the_node(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        publisher = _Publisher({"setting": "first"}, {"setting": "second"})
        saves = _Saves(engine)

        _publish(engine, workflow, _publisher(publisher), saves)
        result = _publish(engine, workflow, _publisher(publisher), saves)

        assert isinstance(result, PublishWorkflowResultSuccess), result.result_details
        assert publisher.received == [None, {"setting": "first"}]
        nodes = _configuration_nodes(engine, workflow)
        assert [node.name for node in nodes] == ["Target Configuration"]
        assert nodes[0].get_parameter_value("setting") == "second"

    def test_returning_no_configuration_leaves_the_remembered_values_alone(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        publisher = _Publisher({"setting": "first"}, None)
        saves = _Saves(engine)

        _publish(engine, workflow, _publisher(publisher), saves)
        _publish(engine, workflow, _publisher(publisher), saves)

        assert _configuration_nodes(engine, workflow)[0].get_parameter_value("setting") == "first"

    def test_a_target_without_a_configuration_node_type_gets_none(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        publisher = _Publisher({"setting": "ignored"})
        saves = _Saves(engine)

        result = _publish(engine, workflow, _publisher(publisher, configuration_node_type=None), saves)

        assert isinstance(result, PublishWorkflowResultSuccess), result.result_details
        assert _configuration_nodes(engine, workflow) == []
        assert len(saves.configuration_nodes_at_save) == 1

    def test_a_value_the_node_does_not_have_is_reported_without_failing_the_publish(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        publisher = _Publisher({"setting": "first", "no_such_parameter": "x"})

        result = _publish(engine, workflow, _publisher(publisher), _Saves(engine))

        assert isinstance(result, PublishWorkflowResultSuccess), result.result_details
        assert "no_such_parameter" in str(result.result_details)
        assert _configuration_nodes(engine, workflow)[0].get_parameter_value("setting") == "first"

    def test_the_publish_dialog_is_handed_the_remembered_values(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        _publish(engine, workflow, _publisher(_Publisher({"setting": "first"})), _Saves(engine))
        seen: list[dict[str, Any] | None] = []

        def get_publish_options(request: GetPublishOptionsRequest) -> GetPublishOptionsResultSuccess:
            seen.append(request.configuration)
            return GetPublishOptionsResultSuccess(fields=[], result_details="options")

        name = engine.workflow_registry.get(workflow.workflow_id).name
        with patch.object(
            engine.library_manager,
            "get_registered_event_handlers",
            return_value={"lib": _publisher(MagicMock(), get_publish_options)},
        ):
            result = _send(engine, GetPublishOptionsRequest(workflow_name=name, publisher_name="lib"), workflow)

        assert isinstance(result, GetPublishOptionsResultSuccess), result.result_details
        assert seen == [{"setting": "first"}]


class TestAConfigurationNodeCannotBeCopied:
    def test_copying_a_selection_that_holds_one_is_refused(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _create(engine, workflow, "_TargetConfiguration", "Target Configuration")

        result = _send(
            engine,
            SerializeSelectedNodesToCommandsRequest(
                nodes_to_serialize=[["Consumer", "1"], ["Target Configuration", "2"]]
            ),
            workflow,
        )

        assert isinstance(result, SerializeSelectedNodesToCommandsResultFailure)
        assert "Target Configuration" in str(result.result_details)

    def test_duplicating_one_is_refused(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _create(engine, workflow, "_TargetConfiguration", "Target Configuration")

        result = _send(
            engine, DuplicateSelectedNodesRequest(nodes_to_duplicate=[["Target Configuration", "1"]]), workflow
        )

        assert isinstance(result, DuplicateSelectedNodesResultFailure)
        assert len(_configuration_nodes(engine, workflow)) == 1
