"""Contract tests for NodeExecutor helpers and SubflowNodeGroup execute() branches.

These tests describe the observable contract of:

* ``NodeExecutor.get_workflow_handler`` - returns the registered handler for a
  library, or raises ``ValueError`` with the library name in the message.
* ``NodeExecutor._deserialize_parameter_value`` - resolves UUID-referenced
  pickled bytes back into Python objects. By the time this function is called,
  cattrs has already decoded base85-encoded bytes via the typed
  ``ControlFlowResolvedEvent.unique_parameter_uuid_to_values`` field, so
  ``stored_value`` is always ``bytes``.
* ``NodeExecutor._extract_parameter_output_values`` - merges per-node output
  dicts from a subprocess result, using the deserializer for UUID references
  and falling back to a pre-mapping flat shape for backward compatibility.
* ``NodeExecutor.execute`` - the remaining ``SubflowNodeGroup`` branches
  (private execution, library-name execution) and the unexpected-result-type
  edge case.
"""

import pickle
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION, PRIVATE_EXECUTION
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRequest

_GRIPTAPE_NODES_PATH = "griptape_nodes.common.node_executor.GriptapeNodes"


def _make_executor() -> NodeExecutor:
    return NodeExecutor.__new__(NodeExecutor)


def _make_subflow_node(execution_type: str) -> MagicMock:
    node = MagicMock(spec=SubflowNodeGroup)
    node.name = "Subflow"
    node.execution_environment = MagicMock()
    node.execution_environment.name = "execution_environment"
    node.get_parameter_value = MagicMock(return_value=execution_type)
    node.aprocess = AsyncMock()
    node.subflow_execution_component = MagicMock()
    node.subflow_execution_component.clear_execution_state = MagicMock()
    return node


class TestGetWorkflowHandler:
    """get_workflow_handler returns the PublishWorkflowRequest handler for a library."""

    def test_returns_registered_handler_for_known_library(self) -> None:
        sentinel_handler = object()
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_lm = MagicMock()
            mock_lm.get_registered_event_handlers.return_value = {"my_lib": sentinel_handler}
            mock_gn.LibraryManager.return_value = mock_lm

            handler = _make_executor().get_workflow_handler("my_lib")

        assert handler is sentinel_handler
        mock_lm.get_registered_event_handlers.assert_called_once_with(PublishWorkflowRequest)

    def test_raises_value_error_when_library_unregistered(self) -> None:
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_lm = MagicMock()
            mock_lm.get_registered_event_handlers.return_value = {}
            mock_gn.LibraryManager.return_value = mock_lm

            with pytest.raises(ValueError, match="missing_lib"):
                _make_executor().get_workflow_handler("missing_lib")


class TestDeserializeParameterValue:
    """_deserialize_parameter_value resolves UUID-referenced pickled bytes.

    By the time this function runs, ``ControlFlowResolvedEvent`` has already been
    structured by cattrs (preconf.json), which auto-decodes base85-encoded strings
    back to real ``bytes``. The stored value is therefore always ``bytes``.
    """

    def test_returns_param_value_unchanged_when_not_a_uuid_reference(self) -> None:
        executor = _make_executor()
        sentinel = object()

        result = executor._deserialize_parameter_value(
            param_name="x",
            param_value=sentinel,
            unique_uuid_to_values={"some-uuid": pickle.dumps("irrelevant")},
        )

        assert result is sentinel

    def test_unpickles_bytes_stored_under_uuid(self) -> None:
        executor = _make_executor()
        original = {"answer": 42, "items": [1, 2, 3]}

        result = executor._deserialize_parameter_value(
            param_name="payload",
            param_value="uuid-1",
            unique_uuid_to_values={"uuid-1": pickle.dumps(original)},
        )

        assert result == original

    def test_unpickles_arbitrary_python_objects(self) -> None:
        executor = _make_executor()
        original = complex(3, 7)

        result = executor._deserialize_parameter_value(
            param_name="point",
            param_value="uuid-pt",
            unique_uuid_to_values={"uuid-pt": pickle.dumps(original)},
        )

        assert result == original


class TestExtractParameterOutputValues:
    """_extract_parameter_output_values merges per-node outputs from a subprocess result."""

    def test_returns_empty_dict_for_empty_input(self) -> None:
        assert _make_executor()._extract_parameter_output_values({}) == {}

    def test_passes_through_values_when_no_uuid_mapping(self) -> None:
        executor = _make_executor()
        subprocess_result: dict[str, Any] = {
            "node_a": {"parameter_output_values": {"out1": 1, "out2": "two"}},
        }

        assert executor._extract_parameter_output_values(subprocess_result) == {"out1": 1, "out2": "two"}

    def test_deserializes_uuid_referenced_values(self) -> None:
        executor = _make_executor()
        original = [10, 20, 30]
        subprocess_result: dict[str, Any] = {
            "node_a": {
                "parameter_output_values": {"items": "uuid-1"},
                "unique_parameter_uuid_to_values": {"uuid-1": pickle.dumps(original)},
            },
        }

        assert executor._extract_parameter_output_values(subprocess_result) == {"items": original}

    def test_merges_outputs_from_multiple_result_dicts(self) -> None:
        executor = _make_executor()
        subprocess_result: dict[str, Any] = {
            "node_a": {"parameter_output_values": {"a": 1}},
            "node_b": {"parameter_output_values": {"b": 2}},
        }

        assert executor._extract_parameter_output_values(subprocess_result) == {"a": 1, "b": 2}

    def test_backward_compatible_with_flat_result_shape(self) -> None:
        """Old flat structure: result dict directly contains output keys."""
        executor = _make_executor()
        subprocess_result: dict[str, Any] = {
            "node_a": {"out1": "hello", "out2": "world"},
        }

        assert executor._extract_parameter_output_values(subprocess_result) == {"out1": "hello", "out2": "world"}


class TestExecuteSubflowNodeGroupBranches:
    """SubflowNodeGroup non-local branches delegate to dedicated workflow paths."""

    @pytest.mark.asyncio
    async def test_private_execution_calls_private_workflow_path(self) -> None:
        node = _make_subflow_node(PRIVATE_EXECUTION)

        with (
            patch(_GRIPTAPE_NODES_PATH) as mock_gn,
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock) as mock_library,
        ):
            mock_gn.ahandle_request = AsyncMock()
            await _make_executor().execute(node)

        mock_private.assert_awaited_once_with(node)
        mock_library.assert_not_awaited()
        node.aprocess.assert_not_awaited()
        mock_gn.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_library_execution_routes_through_library_workflow(self) -> None:
        """A non-Local, non-Private execution_environment is treated as a library name."""
        node = _make_subflow_node("some_library_name")

        with (
            patch(_GRIPTAPE_NODES_PATH) as mock_gn,
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock) as mock_library,
        ):
            mock_gn.ahandle_request = AsyncMock()
            await _make_executor().execute(node)

        mock_library.assert_awaited_once_with(node, "some_library_name")
        mock_private.assert_not_awaited()
        node.aprocess.assert_not_awaited()
        mock_gn.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subprocess_paths_clear_execution_state_first(self) -> None:
        """Both PRIVATE and library paths must clear execution state before running."""
        node = _make_subflow_node(PRIVATE_EXECUTION)

        with (
            patch(_GRIPTAPE_NODES_PATH),
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock),
        ):
            await _make_executor().execute(node)

        node.subflow_execution_component.clear_execution_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_local_execution_does_not_clear_execution_state(self) -> None:
        """LOCAL_EXECUTION runs aprocess directly; clearing subprocess state is unnecessary."""
        node = _make_subflow_node(LOCAL_EXECUTION)

        with patch(_GRIPTAPE_NODES_PATH):
            await _make_executor().execute(node)

        node.subflow_execution_component.clear_execution_state.assert_not_called()


class TestExecuteUnexpectedResultType:
    """Anything that isn't an ExecuteNodeResultSuccess is surfaced as a RuntimeError."""

    @pytest.mark.asyncio
    async def test_raises_when_result_is_not_a_success_payload(self) -> None:
        node = MagicMock()
        node.name = "Weird"
        node.parameter_values = {}
        node.parameter_output_values = {}
        node.metadata = {}

        not_a_payload: Any = "not a payload at all"

        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=not_a_payload)

            with pytest.raises(RuntimeError, match="Weird"):
                await _make_executor().execute(node)


class TestExecuteSuccessReturnsNone:
    """The contract of execute() is to return None; all output flows via parameter_output_values."""

    @pytest.mark.asyncio
    async def test_returns_none_on_success(self) -> None:
        node = MagicMock()
        node.name = "Plain"
        node.parameter_values = {}
        node.parameter_output_values = {}
        node.metadata = {}

        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.ahandle_request = AsyncMock(
                return_value=ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={"x": 1}),
            )
            result = await _make_executor().execute(node)

        assert result is None


class TestFormatNodeFailureMessage:
    """Worker-side traceback frames have to land in the RuntimeError message.

    Chaining via ``raise RuntimeError(msg) from exc`` is not enough on
    its own: a ForwardedException is constructed (not raised) on the
    receiving side, so its ``__traceback__`` is None and Python's
    chained-exception display prints only the cause's
    ``Type: message`` line. The helper interpolates
    ``original_traceback`` so the worker frames are actually visible.
    """

    def test_includes_original_type_prefix(self) -> None:
        from griptape_nodes.retained_mode.events.base_events import ForwardedException

        exc = ForwardedException("rebuilt", original_type="builtins.RuntimeError")

        msg = NodeExecutor._format_node_failure_message("MyNode", MagicMock(result_details="oops"), exc)

        assert "[builtins.RuntimeError]" in msg
        assert "MyNode" in msg

    def test_appends_worker_traceback_when_present(self) -> None:
        from griptape_nodes.retained_mode.events.base_events import ForwardedException

        exc = ForwardedException(
            "rebuilt",
            original_type="builtins.ValueError",
            original_traceback='Traceback...\n  File "a.py", line 1, in <module>\nValueError: rebuilt\n',
        )

        msg = NodeExecutor._format_node_failure_message("MyNode", MagicMock(result_details="oops"), exc)

        assert "Worker traceback:" in msg
        assert 'File "a.py"' in msg

    def test_omits_worker_block_for_local_exceptions(self) -> None:
        # Plain Exception (not a ForwardedException) means we're on the
        # local path. No type prefix, no worker-traceback block.
        msg = NodeExecutor._format_node_failure_message(
            "MyNode", MagicMock(result_details="oops"), RuntimeError("local")
        )

        assert "Worker traceback:" not in msg
        assert "[" not in msg.split("execution failed:")[1].split(":")[0]


class TestControlFlowResolvedEventCattrsRoundTrip:
    """ControlFlowResolvedEvent.unique_parameter_uuid_to_values round-trips through cattrs.

    cattrs.preconf.json registers symmetric base85 hooks for bytes.  When the
    subprocess unstructures a ControlFlowResolvedEvent containing bytes values,
    those values become base85 strings on the wire.  When the parent structures
    the received payload back into a ControlFlowResolvedEvent, cattrs decodes
    the base85 strings back into bytes — so _deserialize_parameter_value always
    sees real bytes.
    """

    def test_bytes_values_survive_unstructure_structure_round_trip(self) -> None:
        from griptape_nodes.retained_mode.events.event_converter import converter, safe_unstructure
        from griptape_nodes.retained_mode.events.execution_events import ControlFlowResolvedEvent
        from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

        original = {"answer": 42}
        pickled = pickle.dumps(original)
        uuid = SerializedNodeCommands.UniqueParameterValueUUID("test-uuid-1")

        event = ControlFlowResolvedEvent(
            end_node_name="EndFlow",
            parameter_output_values={"result": uuid},
            unique_parameter_uuid_to_values={uuid: pickled},
        )

        # Simulate what the subprocess does: unstructure → JSON
        raw = safe_unstructure(event)

        # The bytes value must have been base85-encoded to a string on the wire
        wire_value = raw["unique_parameter_uuid_to_values"][uuid]
        assert isinstance(wire_value, str), "cattrs must base85-encode bytes during unstructure"

        # Simulate what the parent does: JSON → structure
        reconstructed = converter.structure(raw, ControlFlowResolvedEvent)

        # After structuring with the typed field, value must be bytes again
        assert reconstructed.unique_parameter_uuid_to_values is not None
        stored = reconstructed.unique_parameter_uuid_to_values[uuid]
        assert isinstance(stored, bytes), "cattrs must decode base85 back to bytes during structure"
        assert stored == pickled

        # And pickle.loads on the stored bytes must recover the original object
        assert pickle.loads(stored) == original  # noqa: S301
