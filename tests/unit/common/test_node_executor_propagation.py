"""Tests for output copy-back behavior in NodeExecutor.execute().

NodeExecutor.execute dispatches a single ExecuteNodeRequest for both local and
worker execution. After the handler returns, outputs are copied back onto the
in-memory node via parameter_output_values (not set_parameter_value). The
copy-back is idempotent: TrackedParameterOutputValues.__setitem__ guards on
old_value != new_value, so on the local path -- where aprocess already wrote
these entries in place -- no duplicate AlterElementEvent is emitted. On the
worker path the orchestrator stub has not seen the writes, so the copy-back
is the first (and only) emit per key.
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import TrackedParameterOutputValues
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    OriginalNodeParameter,
    PackagedNodeParameterMapping,
    PackageNodesAsSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import ListVariablesRequest, ListVariablesResultSuccess
from griptape_nodes.retained_mode.variable_types import FlowVariable, VariableLayerKind
from tests.unit.exe_types.mocks import MockNode

_EXPECTED_FRESH_OUTPUT_EMITS = 2

# A node built without an engine resolves the ambient one; patch that resolution at the
# source module so the property picks up the stand-in at call time.
_CURRENT_ENGINE_PATCH = "griptape_nodes.retained_mode.engine.current_engine"


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_node_with_tracked_outputs(name: str = "TestNode") -> MagicMock:
    """Mock node with a real TrackedParameterOutputValues so __setitem__ guards run."""
    node = MagicMock()
    node.name = name
    node.parameter_values = {}
    node.parameter_output_values = TrackedParameterOutputValues(node)
    node.metadata = {}
    return node


class TestLocalExecuteCopyBack:
    """Copy-back writes into parameter_output_values, not via set_parameter_value."""

    @pytest.mark.asyncio
    async def test_copy_back_does_not_call_set_parameter_value(self) -> None:
        """Copy-back must not route through BaseNode.set_parameter_value."""
        node = _make_node_with_tracked_outputs()
        result = ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={"out": 42})

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=result)
        await executor.execute(node)

        assert node.parameter_output_values == {"out": 42}
        node.set_parameter_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_copy_back_emits_once_per_key_when_aprocess_already_wrote(self) -> None:
        """Idempotent copy-back: aprocess-writes + copy-back = exactly one emit per key."""
        node = _make_node_with_tracked_outputs()

        # Simulate in-process execution: the handler writes directly onto
        # node.parameter_output_values (via aprocess), then returns a result
        # whose parameter_output_values is a dict copy of that same state.
        async def fake_handle(_req: Any) -> ExecuteNodeResultSuccess:
            node.parameter_output_values["out"] = 42  # first (and should-be-only) emit
            return ExecuteNodeResultSuccess(
                result_details="ok",
                parameter_output_values=dict(node.parameter_output_values),
            )

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)
        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            await executor.execute(node)

        assert mock_emit.call_count == 1
        assert node.parameter_output_values == {"out": 42}

    @pytest.mark.asyncio
    async def test_copy_back_emits_for_fresh_outputs_on_worker_path(self) -> None:
        """Worker path: copy-back is a first-time assignment per key, one emit per key.

        Simulated by returning a result whose outputs are not yet on the node --
        matches the orchestrator's view of a node whose aprocess ran remotely.
        """
        node = _make_node_with_tracked_outputs()

        async def fake_handle(_req: Any) -> ExecuteNodeResultSuccess:
            return ExecuteNodeResultSuccess(
                result_details="ok",
                parameter_output_values={"a": 1, "b": 2},
            )

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)
        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            await executor.execute(node)

        # Two fresh keys; each __setitem__ sees old_value None != new_value.
        assert mock_emit.call_count == _EXPECTED_FRESH_OUTPUT_EMITS
        assert node.parameter_output_values == {"a": 1, "b": 2}


def _make_template_node(name: str = "PromptNode", template: str = "{SHOT}") -> MockNode:
    """Real node whose PROPERTY|OUTPUT parameter holds a variable template."""
    node = MockNode(name=name)
    node.add_parameter(
        Parameter(
            name="prompt",
            default_value=template,
            input_types=["str"],
            output_type="str",
            type="str",
            allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            tooltip="test",
        )
    )
    node.parameter_values["prompt"] = template
    return node


def _make_end_mapping(
    sanitized_name: str, node_name: str, param_name: str
) -> PackageNodesAsSerializedFlowResultSuccess:
    """A package result whose End-node mappings point `sanitized_name` at `node_name.param_name`.

    The mappings are the real NamedTuples rather than mocks: the code under test
    compares `original.node_name` / `.parameter_name` against live values, and a
    MagicMock attribute would satisfy a lookup it should not.
    """
    end_mapping = PackagedNodeParameterMapping(
        node_name="End_Package_MultiNode",
        parameter_mappings={sanitized_name: OriginalNodeParameter(node_name=node_name, parameter_name=param_name)},
    )
    start_mapping = PackagedNodeParameterMapping(node_name="Start_Package_MultiNode", parameter_mappings={})
    return PackageNodesAsSerializedFlowResultSuccess(
        result_details="ok",
        serialized_flow_commands=cast("Any", None),
        workflow_shape=cast("Any", {}),
        packaged_node_names=[node_name],
        parameter_name_mappings=[start_mapping, end_mapping],
    )


def _gn_mock_with_variable(name: str = "SHOT") -> Any:
    """A stand-in engine where `name` is a real, defined flow variable.

    `should_preserve_stored_template` confirms substitution would actually rewrite
    the stored text before declining a write, so these tests have to define a
    variable for the template to name (or use an optional token). A bare
    MagicMock would also let the assertions pass -- the variable lookup would
    return a non-ListVariablesResultSuccess and fall back to trusting the regex
    heuristic -- but then they would no longer be testing the intended path.
    """
    engine = MagicMock()
    engine.workflow_manager.is_variable_substitution_enabled.return_value = True
    engine.node_manager.get_node_parent_flow_by_name.return_value = "test_flow"
    engine.handle_request.side_effect = lambda req: (
        ListVariablesResultSuccess(
            variables=[FlowVariable(name=name, owning_flow_name="test_flow", type="str", value="hyperreal")],
            layers=[VariableLayerKind.FLOW],
            result_details="ok",
        )
        if isinstance(req, ListVariablesRequest)
        else ListConnectionsForNodeResultSuccess(incoming_connections=[], outgoing_connections=[], result_details="ok")
        if isinstance(req, ListConnectionsForNodeRequest)
        else MagicMock()
    )
    # The nodes under test are built without an engine, so they resolve the ambient one.
    return patch(_CURRENT_ENGINE_PATCH, return_value=engine)


class TestGroupCopyBackPreservesTemplate:
    """Group/loop copy-back must never write a resolved value into parameter_values.

    parameter_values is where get_display_value_for_output reads the template
    from, so overwriting it destroys the user's `{VAR}` text permanently -- a
    browser refresh cannot bring it back and a save persists the substituted
    string.
    """

    def test_last_iteration_does_not_overwrite_stored_template(self) -> None:
        node = _make_template_node()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_values["prompt"] == "{SHOT}"

    def test_last_iteration_still_applies_the_output_value(self) -> None:
        """The resolved value must still land in parameter_output_values."""
        node = _make_template_node()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_output_values["prompt"] == "hyperreal"

    def test_last_iteration_still_writes_non_template_values(self) -> None:
        """Parameters without a macro keep the existing write-through behaviour."""
        node = _make_template_node(template="plain text")
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_values["prompt"] == "hyperreal"

    def test_last_iteration_writes_text_that_only_looks_templated(self) -> None:
        """`{color: red}` matches the macro regex but names no variable -- write it.

        Declining here would freeze the stored value permanently: no later group
        run would ever update it.
        """
        node = _make_template_node(template="body {color: red}")
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_values["prompt"] == "hyperreal"

    def test_last_iteration_writes_when_a_format_spec_cannot_apply(self) -> None:
        """`{SHOT:03}` with SHOT="hyperreal" is left verbatim by the resolver -- so write it.

        `NumericPaddingFormat.apply` raises on a non-numeric value and
        `resolve_macro_token` returns the token unchanged. Naming a live variable is
        therefore not enough to call something a template: the write guard has to ask
        the resolver, not just look the name up, or this parameter's stored value would
        be frozen for good.
        """
        node = _make_template_node(template="{SHOT:03}")
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_values["prompt"] == "hyperreal"

    def test_last_iteration_preserves_an_optional_template(self) -> None:
        """`{SHOT?}` resolves to "" when SHOT is undefined -- still the user's template.

        The write guard must not require the name to be *defined*: an optional token
        substitutes either way, so declining to notice it would store the resolved
        text and take the template with it.
        """
        node = _make_template_node(template="{SHOT?}")
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable(name="SOMETHING_ELSE"):
            executor._apply_last_iteration_to_packaged_nodes({"PromptNode_prompt": "hyperreal"}, package_result)

        assert node.parameter_values["prompt"] == "{SHOT?}"
        assert node.parameter_output_values["prompt"] == "hyperreal"

    def test_apply_parameter_values_skips_the_request_for_a_template(self) -> None:
        """The sequential-group copy-back has the same hazard via a set-value request.

        The request is skipped entirely rather than sent with is_output=True: the
        handler gates unresolve_future_nodes on the value having changed, and for an
        output write that is only true when the key already held something different,
        so an is_output request would drop downstream invalidation instead of keeping
        it. The output value still reaches downstream nodes via the direct write.
        """
        node = _make_template_node()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_parameter_values_to_node(node, {"PromptNode_prompt": "hyperreal"}, package_result)

        mock_engine.node_manager.on_set_parameter_value_request.assert_not_called()
        assert node.parameter_values["prompt"] == "{SHOT}"
        assert node.parameter_output_values["prompt"] == "hyperreal"

    def test_apply_parameter_values_still_invalidates_downstream_nodes(self) -> None:
        """Skipping the request must not skip the invalidation the request would have done.

        Without this, a group run leaves downstream nodes resolved against the
        previous value and they never recompute.
        """
        node = _make_template_node()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_parameter_values_to_node(node, {"PromptNode_prompt": "hyperreal"}, package_result)

        unresolve = mock_engine.flow_manager.get_connections.return_value.unresolve_future_nodes
        unresolve.assert_called_once_with(node)

    def test_apply_parameter_values_invalidates_even_when_output_unchanged(self) -> None:
        """Invalidation is unconditional, matching the request this path stands in for.

        The handler gates on the *stored* value changing, and here that is the template
        against the resolved text, so it always unresolved. Comparing the previous
        output instead would be a new optimisation, and it is the kind that leaves a
        node resolved against stale input when it guesses wrong.
        """
        node = _make_template_node()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            # Seeded inside the patch: this assignment goes through
            # TrackedParameterOutputValues.__setitem__, which consults the display
            # guard and emits an event, so it needs the same mocked singletons the
            # call under test does.
            node.parameter_output_values["prompt"] = "hyperreal"
            executor._apply_parameter_values_to_node(node, {"PromptNode_prompt": "hyperreal"}, package_result)

        unresolve = mock_engine.flow_manager.get_connections.return_value.unresolve_future_nodes
        unresolve.assert_called_once_with(node)

    def test_apply_parameter_values_writes_non_template_values_normally(self) -> None:
        """Without a template the request must still be sent as a stored-value write."""
        node = _make_template_node(template="plain text")
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        package_result = _make_end_mapping("PromptNode_prompt", "PromptNode", "prompt")

        with _gn_mock_with_variable():
            executor._apply_parameter_values_to_node(node, {"PromptNode_prompt": "hyperreal"}, package_result)

        request = mock_engine.node_manager.on_set_parameter_value_request.call_args.args[0]
        assert request.is_output is False
        assert request.parameter_name == "prompt"
        assert request.value == "hyperreal"
