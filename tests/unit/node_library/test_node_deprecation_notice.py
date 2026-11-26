"""Node deprecation travels on its own, not as a library problem."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.library_declarations import (
    LifecycleStage,
    LifecycleStageLibraryProperty,
    LifecycleStageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    DeprecatedNodeNotice,
    LibraryMetadata,
    LibrarySchema,
    NodeDefinition,
    NodeDeprecationMetadata,
    NodeMetadata,
    WorkflowNodeDefinition,
)
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultSuccess,
)
from griptape_nodes.utils.version_utils import get_current_version

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


def _node_metadata(display_name: str, **kwargs: Any) -> NodeMetadata:
    return NodeMetadata(category="Test", description="test node", display_name=display_name, **kwargs)


def _node(class_name: str, **kwargs: Any) -> NodeDefinition:
    return NodeDefinition(class_name=class_name, file_path="nodes.py", metadata=_node_metadata(class_name, **kwargs))


def _schema(nodes: list[NodeDefinition], **kwargs: Any) -> LibrarySchema:
    library_declarations = kwargs.pop("library_declarations", [])
    return LibrarySchema(
        name="Deprecation Test Library",
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="test library",
            library_version="1.0.0",
            engine_version=get_current_version().lstrip("v"),
            tags=[],
            declarations=library_declarations,
        ),
        categories=[],
        nodes=nodes,
        **kwargs,
    )


class TestDeprecationNotice:
    def test_metadata_deprecation_gives_a_notice_with_its_message(self) -> None:
        metadata = _node_metadata(
            "Old Node",
            deprecation=NodeDeprecationMetadata(deprecation_message="Use New Node instead.", removal_version="2.0.0"),
        )

        assert metadata.deprecation_notice("OldNode") == DeprecatedNodeNotice(
            node_type="OldNode",
            display_name="Old Node",
            deprecation_message="Use New Node instead.",
            removal_version="2.0.0",
        )

    def test_own_deprecated_stage_gives_a_notice_without_a_message(self) -> None:
        metadata = _node_metadata(
            "Old Node", declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.DEPRECATED)]
        )

        assert metadata.deprecation_notice("OldNode") == DeprecatedNodeNotice(
            node_type="OldNode", display_name="Old Node"
        )

    def test_other_stages_give_no_notice(self) -> None:
        metadata = _node_metadata("Labs Node", declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.LABS)])

        assert metadata.deprecation_notice("LabsNode") is None


class TestLibraryDeprecatedNodes:
    def test_lists_nodes_deprecated_either_way_in_manifest_order(self) -> None:
        schema = _schema(
            [
                _node("ByStage", declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.DEPRECATED)]),
                _node("Current"),
                _node("ByMetadata", deprecation=NodeDeprecationMetadata(deprecation_message="Use Current.")),
            ]
        )

        assert [notice.node_type for notice in schema.deprecated_nodes()] == ["ByStage", "ByMetadata"]

    def test_nodes_that_only_inherit_a_deprecated_library_stage_are_not_listed(self) -> None:
        schema = _schema(
            [_node("Plain")],
            library_declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.DEPRECATED)],
        )

        assert schema.deprecated_nodes() == []

    def test_includes_deprecated_workflow_nodes(self) -> None:
        schema = _schema(
            [],
            workflow_nodes=[
                WorkflowNodeDefinition(
                    node_type="OldWorkflowNode",
                    workflow_path="old.py",
                    metadata=_node_metadata("Old Workflow Node", deprecation=NodeDeprecationMetadata()),
                )
            ],
        )

        assert [notice.node_type for notice in schema.deprecated_nodes()] == ["OldWorkflowNode"]


class TestDeprecationIsNotALibraryProblem:
    def test_version_check_reports_no_problem_for_deprecated_nodes(self, engine: Engine) -> None:
        schema = _schema(
            [
                _node("ByStage", declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.DEPRECATED)]),
                _node("ByMetadata", deprecation=NodeDeprecationMetadata(deprecation_message="Use something else.")),
            ]
        )

        assert engine.version_compatibility_manager.check_library_version_compatibility(schema) == []

    def test_load_result_carries_the_deprecated_nodes(self, engine: Engine, tmp_path: Path) -> None:
        schema = _schema([_node("ByMetadata", deprecation=NodeDeprecationMetadata(deprecation_message="Use New."))])
        manifest = tmp_path / "griptape_nodes_library.json"
        manifest.write_text(json.dumps(schema.model_dump(mode="json")))

        result = engine.library_manager.metadata_loading.read_library_metadata(
            LoadLibraryMetadataFromFileRequest(file_path=str(manifest))
        )

        assert isinstance(result, LoadLibraryMetadataFromFileResultSuccess)
        assert result.deprecated_nodes == [
            DeprecatedNodeNotice(node_type="ByMetadata", display_name="ByMetadata", deprecation_message="Use New.")
        ]
