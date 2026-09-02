"""Tests for workflow metadata collection and image-metadata serialization primitives.

Covers the module that lets a saved image carry a workflow inside its own metadata
(`_serialize_flow`, `_serialize_node`, `collect_workflow_metadata`) and the
`WorkflowMetadata` fields that need custom (de)serialization to survive a TOML header
(`node_types_used`, `workflow_shape`). Deliberately does not cover the sidecar metadata
module (`test_sidecar_metadata.py`) or `ExtractFlowCommandsFromImageMetadata`
(`test_flow_manager.py`), which read this module's output from the other side.
"""

import base64
import json
import pickle
from collections.abc import Generator

import pytest

from griptape_nodes.exe_types.node_types import ErrorProxyNode
from griptape_nodes.node_library.workflow_registry import LibraryNameAndNodeType, WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.context_events import EnsureWorkflowAndFlowRequest
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import (
    METADATA_NAMESPACE,
    _serialize_flow,
    _serialize_node,
    collect_workflow_metadata,
)


@pytest.fixture
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests.

    Yield-based so the teardown clear runs even when the test body fails.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _add_error_proxy_node(engine: Engine, node_name: str) -> ErrorProxyNode:
    """Add a node to the object manager without needing a registered library.

    `ErrorProxyNode` is the engine's own stand-in for a node whose real type could not be
    created, so constructing one directly sidesteps library registration entirely while
    still exercising a real, fully-formed `BaseNode`.
    """
    node = ErrorProxyNode(
        name=node_name,
        original_node_type="SomeOriginalType",
        original_library_name="SomeOriginalLibrary",
        failure_reason="library unavailable in this test",
        metadata={},
    )
    engine.object_manager.add_object_by_name(node.name, node)
    return node


class TestSerializeFlow:
    """`_serialize_flow` packs a flow's commands into a pickle+base64 string for image metadata."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_round_trips_through_pickle_and_base64(self, engine: Engine) -> None:
        """The payload unpickles back into the exact commands a direct request would produce."""
        engine.context_manager.push_workflow(workflow_name="wf_roundtrip")
        created = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="flow_roundtrip", set_as_new_context=True)
        )
        assert isinstance(created, CreateFlowResultSuccess)

        payload = _serialize_flow(engine, flow_name=created.flow_name)
        assert payload is not None

        unpickled = pickle.loads(base64.b64decode(payload))  # noqa: S301

        direct_result = engine.handle_request(
            SerializeFlowToCommandsRequest(flow_name=created.flow_name, include_create_flow_command=False)
        )
        assert isinstance(direct_result, SerializeFlowToCommandsResultSuccess)
        assert unpickled == direct_result.serialized_flow_commands

    @pytest.mark.usefixtures("clean_object_state")
    def test_no_flow_name_and_no_current_flow_returns_none(self, engine: Engine) -> None:
        """With nothing to serialize and no flow context to fall back to, this must not raise."""
        engine.context_manager.push_workflow(workflow_name="wf_no_flow")

        assert not engine.context_manager.has_current_flow()
        assert _serialize_flow(engine, flow_name=None) is None

    @pytest.mark.usefixtures("clean_object_state")
    def test_unknown_flow_name_returns_none(self, engine: Engine) -> None:
        """A flow name that does not exist fails the underlying request; this must degrade to None, not raise."""
        engine.context_manager.push_workflow(workflow_name="wf_missing_flow")

        assert _serialize_flow(engine, flow_name="does_not_exist") is None

    @pytest.mark.usefixtures("clean_object_state")
    def test_none_flow_name_uses_current_flow_context(self, engine: Engine) -> None:
        """Omitting `flow_name` serializes whichever flow is active in the Current Context."""
        engine.context_manager.push_workflow(workflow_name="wf_current_context")
        created = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="flow_current_context", set_as_new_context=True)
        )
        assert isinstance(created, CreateFlowResultSuccess)
        assert engine.context_manager.has_current_flow()

        payload = _serialize_flow(engine, flow_name=None)
        assert payload is not None

        unpickled = pickle.loads(base64.b64decode(payload))  # noqa: S301
        assert unpickled.flow_name is None  # SerializeFlowToCommandsResult doesn't stamp a name for the payload itself


class TestSerializeNode:
    """`_serialize_node` packs a single node's commands into a JSON string for image metadata."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_returns_json_loadable_payload(self, engine: Engine) -> None:
        """The returned string is valid JSON naming the node's original type."""
        engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name="wf_node", flow_name="fl_node"))
        _add_error_proxy_node(engine, "my_node")

        payload = _serialize_node("my_node", engine)
        assert payload is not None

        parsed = json.loads(payload)
        assert parsed["serialized_node_commands"]["create_node_command"]["node_type"] == "SomeOriginalType"

    @pytest.mark.usefixtures("clean_object_state")
    def test_unknown_node_name_returns_none(self, engine: Engine) -> None:
        """A node name that does not exist fails the underlying request; this must degrade to None, not raise."""
        engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name="wf_missing_node", flow_name="fl_missing"))

        assert _serialize_node("no_such_node", engine) is None


class TestCollectWorkflowMetadata:
    """`collect_workflow_metadata` is the entry point that assembles the full metadata dict."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_every_key_is_namespaced(self, engine: Engine) -> None:
        """Every injected key carries the `gtn_` prefix so it never collides with a user's own PNG text chunks."""
        engine.context_manager.push_workflow(workflow_name="wf_namespace")
        created = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="flow_namespace", set_as_new_context=True)
        )
        assert isinstance(created, CreateFlowResultSuccess)

        metadata = collect_workflow_metadata(engine)

        assert metadata  # sanity: something was collected
        assert all(key.startswith(METADATA_NAMESPACE) for key in metadata)

    @pytest.mark.usefixtures("clean_object_state")
    def test_no_workflow_context_still_returns_saved_at(self, engine: Engine) -> None:
        """Even with no workflow in context, a timestamp is always available."""
        assert not engine.context_manager.has_current_workflow()

        metadata = collect_workflow_metadata(engine)

        assert f"{METADATA_NAMESPACE}saved_at" in metadata
        assert f"{METADATA_NAMESPACE}workflow_name" not in metadata

    @pytest.mark.usefixtures("clean_object_state")
    def test_flow_context_embeds_flow_commands_key(self, engine: Engine) -> None:
        """A flow in context contributes its serialized commands under FLOW_COMMANDS_KEY."""
        from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY

        engine.context_manager.push_workflow(workflow_name="wf_flow_commands")
        created = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="flow_flow_commands", set_as_new_context=True)
        )
        assert isinstance(created, CreateFlowResultSuccess)

        metadata = collect_workflow_metadata(engine)

        assert FLOW_COMMANDS_KEY in metadata
        unpickled = pickle.loads(base64.b64decode(metadata[FLOW_COMMANDS_KEY]))  # noqa: S301
        assert unpickled.serialized_node_commands == []


class TestWorkflowMetadataNodeTypesUsedSerialization:
    """`node_types_used` has to survive a TOML header, which has no native set or tuple type."""

    def test_serializes_as_sorted_list_of_pairs(self) -> None:
        """Sorted output keeps the header stable across saves instead of churning on set iteration order."""
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.1.0",
            node_libraries_referenced=[],
            node_types_used={
                LibraryNameAndNodeType("LibB", "TypeZ"),
                LibraryNameAndNodeType("LibA", "TypeY"),
            },
        )

        dumped = metadata.model_dump(mode="json")

        assert dumped["node_types_used"] == [["LibA", "TypeY"], ["LibB", "TypeZ"]]

    def test_empty_set_serializes_as_empty_list(self) -> None:
        """A workflow with no nodes must not crash the header write."""
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.1.0",
            node_libraries_referenced=[],
        )

        assert metadata.model_dump(mode="json")["node_types_used"] == []

    def test_list_of_pairs_round_trips_back_to_a_set(self) -> None:
        """Loading a TOML-shaped list of pairs back must rebuild the original set of pairs."""
        loaded = WorkflowMetadata.model_validate(
            {
                "name": "wf",
                "schema_version": WorkflowMetadata.LATEST_SCHEMA_VERSION,
                "engine_version_created_with": "0.1.0",
                "node_libraries_referenced": [],
                "node_types_used": [["LibA", "TypeY"], ["LibB", "TypeZ"]],
            }
        )

        assert loaded.node_types_used == {
            LibraryNameAndNodeType("LibA", "TypeY"),
            LibraryNameAndNodeType("LibB", "TypeZ"),
        }


class TestWorkflowMetadataWorkflowShapeSerialization:
    """`workflow_shape` is stored as a JSON string because TOML chokes on nested None values."""

    def test_none_shape_serializes_to_none(self) -> None:
        """A workflow with no Start/End nodes has no shape, and that must stay `None`, not `"null"`."""
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.1.0",
            node_libraries_referenced=[],
            workflow_shape=None,
        )

        assert metadata.model_dump(mode="json")["workflow_shape"] is None

    def test_shape_with_none_default_value_round_trips(self) -> None:
        """A parameter whose default is meaningfully `None` must survive as `null`, not vanish."""
        shape = WorkflowShape(inputs={"start_node": {"my_param": {"default_value": None}}})
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.1.0",
            node_libraries_referenced=[],
            workflow_shape=shape,
        )

        dumped = metadata.model_dump(mode="json")
        assert isinstance(dumped["workflow_shape"], str)
        assert json.loads(dumped["workflow_shape"]) == {
            "inputs": {"start_node": {"my_param": {"default_value": None}}},
            "outputs": {},
        }

        reloaded = WorkflowMetadata.model_validate(dumped)
        assert reloaded.workflow_shape == shape


class TestWorkflowMetadataRoundTrip:
    """The whole `WorkflowMetadata` model must survive a dump/reload cycle intact."""

    def test_schema_version_and_scalar_fields_survive_round_trip(self) -> None:
        """A representative set of scalar fields, including the schema version, comes back unchanged."""
        metadata = WorkflowMetadata(
            name="my_workflow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="1.2.3",
            node_libraries_referenced=[],
            description="a test workflow",
            is_template=True,
            is_internal=False,
        )

        dumped = metadata.model_dump(mode="json")
        reloaded = WorkflowMetadata.model_validate(dumped)

        assert reloaded.schema_version == WorkflowMetadata.LATEST_SCHEMA_VERSION
        assert reloaded.name == "my_workflow"
        assert reloaded.engine_version_created_with == "1.2.3"
        assert reloaded.description == "a test workflow"
        assert reloaded.is_template is True
        assert reloaded.is_internal is False
