"""Undo and redo of workflow edits, driven the way a client drives them: requests naming an undo group."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import BaseNodeElement, Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, DataNode, NodeResolutionState
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.app_events import WorkflowStateChangedEvent
from griptape_nodes.retained_mode.events.base_events import AppEvent, RequestPayload, ResultDetails
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import ResolveNodeRequest
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    DeleteNodeRequest,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.undo_events import (
    RedoRequest,
    RedoResultFailure,
    RedoResultSuccess,
    UndoRequest,
    UndoResultFailure,
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    GetVariableValueRequest,
    SetVariableValueRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.undo import manager as manager_module
from griptape_nodes.serialization.commands import encode_commands
from griptape_nodes.serialization.values import dump_json
from griptape_nodes.traits.slider import Slider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY = "undo-test-library"


class _PassThrough(DataNode):
    """Copies its input to its output when it runs."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(name="result", type="str", default_value=None, tooltip="", allowed_modes={ParameterMode.OUTPUT})
        )
        self.add_parameter(
            Parameter(name="volume", type="int", default_value=0, tooltip="", traits={Slider(min_val=0, max_val=10)})
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = self.get_parameter_value("value")


class _Group(SubflowNodeGroup):
    """A group that keeps its members in a flow of their own."""

    async def aprocess(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _library() -> Iterator[None]:
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(_PassThrough, NodeMetadata(category="t", description="d", display_name="Pass"))
    library.register_new_node_type(_Group, NodeMetadata(category="t", description="d", display_name="Group"))
    yield
    LibraryRegistry._clear()


@pytest.fixture
def workflow(engine: Engine) -> Iterator[WorkflowRuntime]:
    """A fresh open workflow with an empty flow, closed after the test."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="undo"))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    yield engine.open_workflows.get(result.workflow_id)
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _send(engine: Engine, workflow: WorkflowRuntime, request: RequestPayload, group: str | None = None) -> Any:
    """Dispatch ``request`` as a client would, in undo group ``group``, and return its result."""
    request.undo_group = group
    context = ResultContext(open_workflow_id=workflow.workflow_id)
    return engine.event_manager.handle_request(request, result_context=context).result


def _ok(engine: Engine, workflow: WorkflowRuntime, request: RequestPayload, group: str | None = None) -> Any:
    result = _send(engine, workflow, request, group)
    assert result.succeeded(), result.result_details
    return result


def _undo(engine: Engine, workflow: WorkflowRuntime) -> UndoResultSuccess:
    result = _send(engine, workflow, UndoRequest())
    assert isinstance(result, UndoResultSuccess), result.result_details
    return result


def _redo(engine: Engine, workflow: WorkflowRuntime) -> RedoResultSuccess:
    result = _send(engine, workflow, RedoRequest())
    assert isinstance(result, RedoResultSuccess), result.result_details
    return result


def _add(engine: Engine, workflow: WorkflowRuntime, name: str, group: str | None = None) -> None:
    _ok(
        engine,
        workflow,
        CreateNodeRequest(node_type="_PassThrough", specific_library_name=_LIBRARY, node_name=name),
        group,
    )


def _set(engine: Engine, workflow: WorkflowRuntime, name: str, value: str, group: str | None = None) -> None:
    _ok(engine, workflow, SetParameterValueRequest(node_name=name, parameter_name="value", value=value), group)


def _node(workflow: WorkflowRuntime, name: str) -> BaseNode | None:
    return workflow.objects.get_as_type(name, BaseNode)


def _element_ids(node: BaseNode) -> list[str]:
    """The ids of a node's elements, its parameters' traits among them, in the order the node holds them."""
    return [element.element_id for element in node.root_ui_element.find_elements_by_type(BaseNodeElement)]


def _value(workflow: WorkflowRuntime, name: str) -> Any:
    node = _node(workflow, name)
    assert node is not None, f"'{name}' is gone"
    return node.get_parameter_value("value")


def _ran(workflow: WorkflowRuntime, *names: str) -> None:
    """Leave each node as a run would: resolved, with its output."""
    for name in names:
        node = _node(workflow, name)
        assert node is not None
        node.parameter_output_values["result"] = f"{name} ran"
        node.state = NodeResolutionState.RESOLVED


class TestUndoAndRedo:
    def test_undo_takes_back_a_change_and_redo_puts_it_back(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")

        undone = _undo(engine, workflow)

        assert undone.label == "Set Parameter Value"
        assert _value(workflow, "a") == ""

        redone = _redo(engine, workflow)

        assert redone.label == "Set Parameter Value"
        assert _value(workflow, "a") == "first"

    def test_requests_in_one_group_undo_as_one_step(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _set(engine, workflow, "a", "second", group="type")
        _set(engine, workflow, "a", "third", group="type")

        _undo(engine, workflow)

        assert _value(workflow, "a") == ""
        assert workflow.undo.undo_label is None

    def test_steps_undo_one_at_a_time(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="one")
        _set(engine, workflow, "a", "second", group="two")

        _undo(engine, workflow)
        assert _value(workflow, "a") == "first"
        _undo(engine, workflow)
        assert _value(workflow, "a") == ""

    def test_a_deleted_node_comes_back_with_its_connections(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _set(engine, workflow, "a", "kept")
        _ok(
            engine,
            workflow,
            CreateConnectionRequest(
                source_node_name="a",
                source_parameter_name="result",
                target_node_name="b",
                target_parameter_name="value",
            ),
        )
        _ok(engine, workflow, DeleteNodeRequest(node_name="a"), group="delete")

        _undo(engine, workflow)

        assert _value(workflow, "a") == "kept"
        assert len(workflow.flows.connections.connections) == 1

    def test_undoing_a_new_node_removes_it(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a", group="add")

        _undo(engine, workflow)

        assert _node(workflow, "a") is None

    def test_the_workflow_matches_its_checkpoint_after_an_undo(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """So the next step starts without checkpointing again."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")

        _undo(engine, workflow)

        assert not workflow.undo.needs_checkpoint

    def test_the_flow_keeps_its_name(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        flow = workflow.top_level_flow()
        assert flow is not None
        _add(engine, workflow, "a", group="add")

        _undo(engine, workflow)

        rebuilt = workflow.top_level_flow()
        assert rebuilt is not None
        assert rebuilt.name == flow.name

    def test_elements_keep_their_ids(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        before = _node(workflow, "a")
        assert before is not None
        _set(engine, workflow, "a", "x", group="set")

        _undo(engine, workflow)

        after = _node(workflow, "a")
        assert after is not None
        assert after is not before
        assert _element_ids(after) == _element_ids(before)

    def test_locking_a_node_is_undone(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _ok(engine, workflow, SetLockNodeStateRequest(node_name="a", lock=True), group="lock")

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert not a.lock

    def test_a_label_the_client_sends_names_the_step(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        request = SetNodeMetadataRequest(node_name="a", metadata={"position": {"x": 1, "y": 1}}, undo_label="Move a")

        _ok(engine, workflow, request, group="drag")

        assert workflow.undo.undo_label == "Move a"
        assert _undo(engine, workflow).label == "Move a"


class TestGroups:
    def _group(self, engine: Engine, workflow: WorkflowRuntime, *members: str, group: str | None = None) -> None:
        request = CreateNodeRequest(
            node_type="_Group", specific_library_name=_LIBRARY, node_name="group", node_names_to_add=list(members)
        )
        _ok(engine, workflow, request, group)

    def test_undoing_a_group_puts_its_members_back(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "kept")
        self._group(engine, workflow, "a", group="group")
        a = _node(workflow, "a")
        assert a is not None
        assert a.parent_group is not None

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.parent_group is None
        assert _node(workflow, "group") is None
        assert _value(workflow, "a") == "kept"

        _redo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.parent_group is _node(workflow, "group")

    def test_a_change_inside_a_group_is_undone(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        self._group(engine, workflow, "a")
        _set(engine, workflow, "a", "inside", group="type")

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.parent_group is _node(workflow, "group")
        assert _value(workflow, "a") == ""

    def test_a_member_the_step_did_not_touch_keeps_its_results(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        self._group(engine, workflow, "a")
        _set(engine, workflow, "b", "outside", group="type")
        _ran(workflow, "a")

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.state is NodeResolutionState.RESOLVED
        assert a.parameter_output_values["result"] == "a ran"


class TestAStepThatChangesNothing:
    def test_is_left_out(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """A client can resend a value unchanged, such as when a field loses focus."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _set(engine, workflow, "a", "first", group="leave the field")
        _set(engine, workflow, "a", "first", group="next input")

        _undo(engine, workflow)

        assert _value(workflow, "a") == ""

    def test_the_same_workflow_checkpoints_the_same(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _ok(
            engine,
            workflow,
            CreateConnectionRequest(
                source_node_name="a",
                source_parameter_name="result",
                target_node_name="b",
                target_parameter_name="value",
            ),
        )
        first = engine.undo_manager._serialize(workflow)
        second = engine.undo_manager._serialize(workflow)
        assert first is not None
        assert second is not None

        assert _checkpoint_text(first) == _checkpoint_text(second)


def _checkpoint_text(commands: SerializedFlowCommands) -> str:
    return dump_json(encode_commands(commands))


class TestWhatIsNotAStep:
    def test_a_request_without_a_group_is_undone_with_the_step_before(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _set(engine, workflow, "a", "outside")

        _undo(engine, workflow)

        assert _value(workflow, "a") == ""

    def test_a_run_is_not_a_step(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """Clicking Run is an input like any other, but a run changes results, not what the user edited."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        ran = _send(engine, workflow, ResolveNodeRequest(node_name="a"), group="click run")
        assert ran.succeeded(), ran.result_details
        a = _node(workflow, "a")
        assert a is not None
        assert a.parameter_output_values.get("result") == "first"

        assert workflow.undo.undo_label == "Set Parameter Value"
        _undo(engine, workflow)

        assert _value(workflow, "a") == ""

    def test_a_change_that_does_not_mark_the_workflow_unsaved_is_kept(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        """A load keeps the workflow from looking unsaved, but undo still has to know of it."""
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _set(engine, workflow, "a", "first", group="one")
        # A request of a new group checkpoints the step before, even when it changes nothing.
        _send(engine, workflow, SetParameterValueRequest(node_name="missing", parameter_name="value", value=""), "two")
        with engine.workflow_manager.squelch_workflow_altered():
            _set(engine, workflow, "b", "loaded")
        _set(engine, workflow, "a", "second", group="three")

        _undo(engine, workflow)

        assert _value(workflow, "a") == "first"
        assert _value(workflow, "b") == "loaded"

    def test_changes_before_the_first_step_are_kept(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")

        _undo(engine, workflow)

        assert _node(workflow, "a") is not None
        assert _send(engine, workflow, UndoRequest()).failed()

    def test_workflow_variables_are_left_as_they_are(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """They belong to the workflow, not to its flow, so rebuilding the flow does not touch them."""
        _add(engine, workflow, "a")
        _ok(engine, workflow, CreateVariableRequest(name="count", type="str", value="one"))
        _set(engine, workflow, "a", "first", group="type")
        _ok(engine, workflow, SetVariableValueRequest(name="count", value="two"))

        _undo(engine, workflow)

        assert _value(workflow, "a") == ""
        assert _ok(engine, workflow, GetVariableValueRequest(name="count")).value == "two"


class TestAClientCatchingUpAfterAnUndo:
    def test_a_change_that_leaves_the_workflow_as_it_was_keeps_what_could_be_redone(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        """An editor that writes back what it shows after an undo sends it without a group."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _undo(engine, workflow)

        written_back = _ok(engine, workflow, SetParameterValueRequest(node_name="a", parameter_name="value", value=""))

        assert written_back.altered_workflow_state
        assert workflow.undo.redo_label == "Set Parameter Value"
        _redo(engine, workflow)
        assert _value(workflow, "a") == "first"

    def test_a_run_since_the_undo_is_not_a_change(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """A rebuild carries over the latest results, so a redo loses nothing a run changed."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _undo(engine, workflow)
        _ran(workflow, "a")

        _ok(engine, workflow, SetParameterValueRequest(node_name="a", parameter_name="value", value=""))

        assert workflow.undo.redo_label == "Set Parameter Value"

    def test_a_change_drops_what_could_be_redone(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """A redo would rebuild the workflow without the change, so it is the redo that goes."""
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _undo(engine, workflow)

        _ok(engine, workflow, SetNodeMetadataRequest(node_name="a", metadata={"size": 1}))
        _add(engine, workflow, "b")

        assert workflow.undo.redo_label is None
        assert isinstance(_send(engine, workflow, RedoRequest()), RedoResultFailure)
        a = _node(workflow, "a")
        assert a is not None
        assert a.metadata["size"] == 1
        assert _node(workflow, "b") is not None

    def test_the_next_input_is_a_step_again(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        _send(engine, workflow, UndoRequest(), group="press undo")

        _set(engine, workflow, "a", "second", group="type again")

        assert workflow.undo.undo_label == "Set Parameter Value"
        assert workflow.undo.redo_label is None


class TestNewStepsAfterAnUndo:
    def test_a_new_step_drops_what_could_be_redone(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="one")
        _undo(engine, workflow)

        _set(engine, workflow, "a", "other", group="two")

        result = _send(engine, workflow, RedoRequest())
        assert isinstance(result, RedoResultFailure)

    def test_the_group_of_the_undone_step_starts_a_new_step(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="one")
        _undo(engine, workflow)

        _set(engine, workflow, "a", "again", group="one")
        _undo(engine, workflow)

        assert _value(workflow, "a") == ""


class TestRunResults:
    def test_nodes_the_step_did_not_touch_keep_their_results(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _set(engine, workflow, "a", "first", group="type")
        _ran(workflow, "a", "b")
        _set(engine, workflow, "b", "edited", group="edit b")

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.state is NodeResolutionState.RESOLVED
        assert a.parameter_output_values["result"] == "a ran"

    def test_a_changed_node_and_its_downstream_run_again(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _add(engine, workflow, "c")
        _ok(
            engine,
            workflow,
            CreateConnectionRequest(
                source_node_name="a",
                source_parameter_name="result",
                target_node_name="b",
                target_parameter_name="value",
            ),
        )
        _set(engine, workflow, "a", "first", group="type")
        _ran(workflow, "a", "b", "c")

        _undo(engine, workflow)

        for name, state in (
            ("a", NodeResolutionState.UNRESOLVED),
            ("b", NodeResolutionState.UNRESOLVED),
            ("c", NodeResolutionState.RESOLVED),
        ):
            node = _node(workflow, name)
            assert node is not None
            assert node.state is state, name

    def test_moving_a_node_back_keeps_its_results(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        _add(engine, workflow, "a")
        _ran(workflow, "a")
        _ok(engine, workflow, SetNodeMetadataRequest(node_name="a", metadata={"position": {"x": 9, "y": 9}}), "drag")

        _undo(engine, workflow)

        a = _node(workflow, "a")
        assert a is not None
        assert a.metadata.get("position") != {"x": 9, "y": 9}
        assert a.state is NodeResolutionState.RESOLVED


class TestWhenUndoIsUnavailable:
    def test_with_nothing_to_undo_it_says_so_quietly(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        result = _send(engine, workflow, UndoRequest())

        assert isinstance(result, UndoResultFailure)
        assert isinstance(result.result_details, ResultDetails)
        assert result.result_details.result_details[0].level == logging.INFO

    def test_while_the_workflow_runs(
        self, engine: Engine, workflow: WorkflowRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")
        monkeypatch.setattr(workflow.flows, "run_in_progress", object())

        result = _send(engine, workflow, UndoRequest())

        assert isinstance(result, UndoResultFailure)
        assert "running" in str(result.result_details)
        assert _value(workflow, "a") == "first"


class TestAFailedRebuild:
    def test_puts_the_workflow_back_and_forgets_the_history(
        self, engine: Engine, workflow: WorkflowRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(engine, workflow, "a")
        _set(engine, workflow, "a", "first", group="type")

        def unbuildable(target: SerializedFlowCommands, live: SerializedFlowCommands) -> SerializedFlowCommands:
            del live
            create_command = target.serialized_node_commands[0].create_node_command
            create_command.node_type = "NoSuchNode"
            create_command.create_error_proxy_on_failure = False
            return target

        monkeypatch.setattr(manager_module, "carry_over_run_state", unbuildable)

        result = _send(engine, workflow, UndoRequest())

        assert isinstance(result, UndoResultFailure)
        assert _value(workflow, "a") == "first"
        assert workflow.undo.undo_label is None


class TestClientsAreTold:
    def test_nothing_of_the_rebuild(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """They read the workflow again after the undo, rather than follow every node coming back."""
        _add(engine, workflow, "a")
        _add(engine, workflow, "b")
        _set(engine, workflow, "a", "first", group="type")
        events: asyncio.Queue = asyncio.Queue()
        engine.event_manager.initialize_queue(events)

        _undo(engine, workflow)

        broadcast = []
        while not events.empty():
            event = events.get_nowait()
            result = getattr(getattr(event, "wrapped_event", None), "result", None)
            if result is not None:
                broadcast.append(type(result).__name__)
        assert broadcast == []

    def test_what_undo_and_redo_would_do(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        events: asyncio.Queue = asyncio.Queue()
        engine.event_manager.initialize_queue(events)
        _add(engine, workflow, "a")

        _set(engine, workflow, "a", "first", group="type")
        _undo(engine, workflow)

        states = []
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, AppEvent) and isinstance(event.payload, WorkflowStateChangedEvent):
                states.append((event.payload.state.undo_label, event.payload.state.redo_label))
        assert ("Set Parameter Value", None) in states
        assert states[-1] == (None, "Set Parameter Value")
