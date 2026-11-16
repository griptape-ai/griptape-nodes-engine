"""`BaseNode.validate_before_workflow_run` re-runs a node only when its `{...}` text renders differently.

What a node's templates render to is recorded when it starts running (`record_templates_for_run`),
and compared before each play or Run. These tests record against a stated variable dict rather than
changing a variable through a request, because the variable requests unresolve readers on their own
and would make the comparison look right whether it works or not.
"""

from unittest.mock import patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.exe_types.variable_resolver import VariableResolver
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.variable_events import CreateVariableRequest, CreateVariableResultSuccess
from tests.unit.exe_types.mocks import MockNode

FLOW = "test_flow"


@pytest.fixture
def flow(engine: Engine) -> None:
    """A clean workflow with one flow, which variable lookups start from."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.context_manager.push_workflow("template_rerun_wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name=FLOW, set_as_new_context=True))
    assert isinstance(result, CreateFlowResultSuccess)


def _variable(engine: Engine, name: str, value: str) -> None:
    result = engine.handle_request(CreateVariableRequest(name=name, type="str", value=value))
    assert isinstance(result, CreateVariableResultSuccess)


def _node(engine: Engine, name: str, text: str = "", *, substitutes: bool = True, in_flow: bool = True) -> MockNode:
    """A RESOLVED node with a `text` input/property and a `result` output.

    `substitutes=False` opts `text` out of variable substitution. `in_flow=False` leaves the node
    out of every flow, so looking up its variables fails.
    """
    node = MockNode(name=name)
    node.add_parameter(
        Parameter(
            name="text",
            tooltip="Text",
            type="str",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            allow_variable_substitution=substitutes,
        )
    )
    node.add_parameter(Parameter(name="result", tooltip="Result", type="str", allowed_modes={ParameterMode.OUTPUT}))
    node.parameter_values["text"] = text
    workflow = engine.current_workflow()
    engine.object_manager.add_object_by_name_in(workflow, name, node)
    if in_flow:
        workflow.nodes.name_to_parent_flow_name[name] = FLOW
    node.state = NodeResolutionState.RESOLVED
    return node


def _connect(engine: Engine, source: MockNode, target: MockNode) -> None:
    source_parameter = source.get_parameter_by_name("result")
    target_parameter = target.get_parameter_by_name("text")
    assert source_parameter is not None
    assert target_parameter is not None
    engine.current_workflow().flows.connections.add_connection(source, source_parameter, target, target_parameter)


@pytest.mark.usefixtures("flow")
class TestTemplateRerun:
    def test_a_node_whose_text_renders_as_it_did_when_it_ran_stays_resolved(self, engine: Engine) -> None:
        _variable(engine, "SHOT", "sc001")
        reader = _node(engine, "reader", "shot {SHOT}")
        reader.record_templates_for_run({"SHOT": "sc001"})

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.RESOLVED

    def test_a_node_whose_text_renders_differently_runs_again_with_its_downstream(self, engine: Engine) -> None:
        _variable(engine, "SHOT", "sc002")
        reader = _node(engine, "reader", "shot {SHOT}")
        downstream = _node(engine, "downstream")
        _connect(engine, reader, downstream)
        reader.record_templates_for_run({"SHOT": "sc001"})

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.UNRESOLVED
        assert downstream.state == NodeResolutionState.UNRESOLVED

    def test_switching_substitution_off_runs_a_node_again(self, engine: Engine) -> None:
        _variable(engine, "SHOT", "sc001")
        reader = _node(engine, "reader", "shot {SHOT}")
        reader.record_templates_for_run({"SHOT": "sc001"})
        engine.current_workflow().variables.substitution_enabled = False

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.UNRESOLVED

    def test_a_node_that_ran_with_substitution_off_stays_resolved_while_it_is_still_off(self, engine: Engine) -> None:
        """Substitution off renders `{title?}` as written, not as the empty string an empty dict would give."""
        reader = _node(engine, "reader", "Hello {title?}")
        reader.record_templates_for_run(None)
        engine.current_workflow().variables.substitution_enabled = False

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.RESOLVED

    def test_a_resolved_node_that_never_ran_this_session_runs_again(self, engine: Engine) -> None:
        _variable(engine, "SHOT", "sc001")
        reader = _node(engine, "reader", "shot {SHOT}")

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.UNRESOLVED

    def test_a_locked_node_keeps_its_frozen_result(self, engine: Engine) -> None:
        _variable(engine, "SHOT", "sc002")
        reader = _node(engine, "reader", "shot {SHOT}")
        reader.record_templates_for_run({"SHOT": "sc001"})
        reader.lock = True

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.RESOLVED

    def test_a_node_without_templates_never_looks_up_variables(self, engine: Engine) -> None:
        reader = _node(engine, "reader", "plain text")

        with patch.object(VariableResolver, "get_variables_without_memoizing") as lookup:
            reader.validate_before_workflow_run()

        lookup.assert_not_called()
        assert reader.state == NodeResolutionState.RESOLVED

    def test_a_parameter_that_opts_out_of_substitution_holds_no_template(self, engine: Engine) -> None:
        """`{SHOT}` in a parameter that substitution skips is plain text, so it never sends the node back."""
        reader = _node(engine, "reader", "shot {SHOT}", substitutes=False)

        with patch.object(VariableResolver, "get_variables_without_memoizing") as lookup:
            reader.validate_before_workflow_run()

        lookup.assert_not_called()
        assert reader.state == NodeResolutionState.RESOLVED

    @pytest.mark.parametrize("cause", ["not in any flow", "substitution off"])
    def test_recording_and_checking_agree_when_there_are_no_variables_to_use(self, engine: Engine, cause: str) -> None:
        """What a run records and what the next check renders come out the same when there are no variables to use.

        `{title?}` is the token that tells them apart: with no variables it renders as written, and
        with an empty variable dict it renders as nothing. Recording one and checking the other would
        send the node back to run on every play.
        """
        reader = _node(engine, "reader", "Hello {title?}", in_flow=cause != "not in any flow")
        if cause == "substitution off":
            engine.current_workflow().variables.substitution_enabled = False
        executor = NodeExecutor(engine=engine)
        reader.record_templates_for_run(executor._resolve_variables_for_node(engine.current_workflow(), "reader"))

        reader.validate_before_workflow_run()

        assert reader.state == NodeResolutionState.RESOLVED
