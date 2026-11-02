"""The open workflow's runtime: how it opens and closes, and the state it holds."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    RenameObjectRequest,
    RenameObjectResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    GetVariableValueRequest,
    GetVariableValueResultFailure,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    SetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledResultFailure,
)
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW
from griptape_nodes.retained_mode.runtime.open_workflow_set import NoOpenWorkflowError
from griptape_nodes.retained_mode.variable_types import VariableScope

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestRuntimeLifecycle:
    """The runtime is open exactly while the context stack holds a workflow."""

    def test_clear_all_object_state_closes_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        assert engine.current_workflow_or_none() is None

    def test_deleting_the_open_workflow_closes_it(self, engine: Engine) -> None:
        name = engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(DeleteWorkflowRequest(name=name))

        assert result.succeeded()
        assert engine.current_workflow_or_none() is None


class TestObjectsOnTheRuntime:
    def test_a_created_flow_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert isinstance(result, CreateFlowResultSuccess)
        assert engine.current_workflow().objects.has("canvas")
        assert engine.object_manager.has_object_with_name("canvas")

    def test_lookups_find_nothing_and_changes_are_refused_with_no_workflow_open(self, engine: Engine) -> None:
        object_manager = engine.object_manager

        assert object_manager.get_filtered_subset() == {}
        assert object_manager.attempt_get_object_by_name("canvas") is None
        assert object_manager.generate_name_for_object("ControlFlow") == "ControlFlow_1"
        with pytest.raises(KeyError):
            object_manager.get_object_by_name("canvas")
        with pytest.raises(NoOpenWorkflowError):
            object_manager.add_object_by_name("thing", object())
        with pytest.raises(NoOpenWorkflowError):
            object_manager.del_obj_by_name("thing")
        assert engine.current_workflow_or_none() is None

    def test_work_bound_to_no_workflow_finds_no_objects_and_cannot_add_one(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        object_manager = engine.object_manager

        with engine.bind_workflow(NO_WORKFLOW):
            assert not object_manager.has_object_with_name("canvas")
            with pytest.raises(NoOpenWorkflowError):
                object_manager.add_object_by_name("thing", object())

        assert object_manager.has_object_with_name("canvas")
        assert not object_manager.has_object_with_name("thing")

    def test_rename_moves_the_object_within_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        result = engine.handle_request(RenameObjectRequest(object_name="canvas", requested_name="renamed"))

        assert isinstance(result, RenameObjectResultSuccess)
        objects = engine.current_workflow().objects
        assert objects.has("renamed")
        assert not objects.has("canvas")

    def test_popping_the_last_entry_drops_objects_left_in_it(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="next")

        assert not engine.object_manager.has_object_with_name("canvas")


class TestVariablesOnTheRuntime:
    def test_global_and_flow_variables_are_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        global_result = engine.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi")
        )
        flow_result = engine.handle_request(
            CreateVariableRequest(name="farewell", type="str", owning_flow="canvas", value="bye")
        )

        assert isinstance(global_result, CreateVariableResultSuccess)
        assert isinstance(flow_result, CreateVariableResultSuccess)
        variables = engine.current_workflow().variables
        assert variables.global_layer.has("greeting")
        assert variables.flow_layers["canvas"].has("farewell")

    def test_creating_a_global_variable_needs_an_open_workflow(self, engine: Engine) -> None:
        result = engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        assert not result.succeeded()
        assert "no workflow is open" in str(result.result_details)
        lookup = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(lookup, GetVariableValueResultFailure)

    def test_work_bound_to_no_workflow_reads_no_variables_and_cannot_create_one(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        with engine.bind_workflow(NO_WORKFLOW):
            lookup = engine.handle_request(
                GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY)
            )
            created = engine.handle_request(
                CreateVariableRequest(name="farewell", type="str", is_global=True, value="bye")
            )

        assert isinstance(lookup, GetVariableValueResultFailure)
        assert not created.succeeded()
        assert not engine.current_workflow().variables.global_layer.has("farewell")

    def test_variables_do_not_survive_clearing_and_reopening(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="next")

        result = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(result, GetVariableValueResultFailure)


class TestSubstitutionFlagOnTheRuntime:
    def test_with_no_workflow_open_the_flag_reads_enabled_and_cannot_be_set(self, engine: Engine) -> None:
        result = engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert isinstance(result, SetVariableSubstitutionEnabledResultFailure)
        assert engine.workflow_manager.variable_substitution.is_enabled() is True

    def test_the_flag_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert engine.current_workflow().variables.substitution_enabled is False
        assert engine.workflow_manager.variable_substitution.is_enabled() is False

    def test_the_flag_does_not_survive_clearing_and_reopening_the_same_workflow(self, engine: Engine) -> None:
        name = engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name=name)

        assert engine.workflow_manager.variable_substitution.is_enabled() is True
