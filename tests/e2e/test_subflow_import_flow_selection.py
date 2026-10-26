"""Regression test for issue #5124: importing a referenced workflow that contains a node group.

A workflow that contains a node group (ForEach, etc.) serializes as MORE THAN ONE flow — its
top-level ``ControlFlow`` plus the group's body flow. When such a workflow is imported as a
referenced sub-flow, ``ReferencedWorkflowImport._execute_workflow_import`` must bind to the TOP-LEVEL
imported flow (the one that holds the Start/End nodes), because that is what the Workflow node
records as its ``subflow_name`` and routes I/O through.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct name so the LibraryManager's LOADED bookkeeping doesn't collide with the other
# in-process subflow tests sharing a worker.
LIBRARY_NAME = "Subflow Import Flow Selection Library"


def _write_grouped_workflow_file(
    engine: Engine, library_json: Path, workflow_path: Path, *, grouped: bool = True, fails: bool = False
) -> None:
    """Write a self-contained workflow ``.py`` to ``workflow_path``.

    Builds a flow whose only node is a group (which owns a nested body flow), or a plain echo node
    when ``grouped`` is False, serializes it to a self-contained module, and clears the in-process
    build so the import starts from a clean slate. When ``fails`` is True the module raises after
    building everything.
    """
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="grouped_inner_workflow")

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        parent_group_name = None
        if grouped:
            group_result = engine.handle_request(
                CreateNodeRequest(
                    node_type="SubflowGroupNode",
                    specific_library_name=LIBRARY_NAME,
                    node_name="SubflowGroup_1",
                    initial_setup=True,
                )
            )
            assert isinstance(group_result, CreateNodeResultSuccess), group_result
            parent_group_name = group_result.node_name
        echo_result = engine.handle_request(
            CreateNodeRequest(
                node_type="EchoNode",
                specific_library_name=LIBRARY_NAME,
                node_name="Echo_1",
                initial_setup=True,
                parent_group_name=parent_group_name,
            )
        )
        assert isinstance(echo_result, CreateNodeResultSuccess), echo_result

    serialize_result = engine.handle_request(
        SerializeFlowToCommandsRequest(flow_name=flow_name, include_create_flow_command=True)
    )
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="grouped_inner_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=None,
    )
    content = engine.workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        workflow_metadata=metadata,
    )
    if fails:
        # The raise goes at the end of `build_workflow()`'s body, which only works while that function
        # is the last statement in the module.
        last_statement = ast.parse(content).body[-1]
        assert isinstance(last_statement, ast.AsyncFunctionDef), last_statement
        assert last_statement.name == "build_workflow", last_statement.name
        content = content.rstrip("\n") + "\n    raise RuntimeError('the workflow file failed')\n"
    workflow_path.write_text(content)

    # Drop the in-process build so the import below starts from a clean parent flow.
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
async def test_import_binds_to_top_level_flow_not_group_body(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Importing a group-containing workflow must return the top-level imported flow."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    workflow_path = tmp_path / "grouped_inner_workflow.py"
    _write_grouped_workflow_file(engine, library_json, workflow_path)

    # Register the workflow file so it can be imported by name.
    import_workflow_result = await engine.ahandle_request(ImportWorkflowRequest(file_path=str(workflow_path)))
    assert isinstance(import_workflow_result, ImportWorkflowResultSuccess), import_workflow_result
    workflow_name = import_workflow_result.workflow_name

    # Fresh parent flow to import into.
    engine.context_manager.push_workflow(workflow_name="parent_workflow")
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    import_result = await engine.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=parent_flow)
    )
    assert isinstance(import_result, ImportWorkflowAsReferencedSubFlowResultSuccess), import_result

    # The imported workflow created two flows (its top-level flow + the group's body flow). The
    # returned flow must be the TOP-LEVEL one, i.e. the flow whose parent is the import target.
    # The group's body flow is parented to that top-level flow, so returning it would be the bug.
    flow_manager = engine.flow_manager
    created_flow = import_result.created_flow_name
    assert flow_manager.get_parent_flow(created_flow) == parent_flow, (
        f"import bound to '{created_flow}' (parent '{flow_manager.get_parent_flow(created_flow)}') "
        f"instead of the top-level flow parented to '{parent_flow}'"
    )


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
class TestFailedImportCleanup:
    """A failed referenced import leaves none of the flows or nodes it had already built."""

    @pytest.fixture
    def created_names(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record the name of every object (flow or node) added to the engine, in order."""
        names: list[str] = []
        object_manager = engine.object_manager
        real_add = object_manager.add_object_by_name

        def add_object_by_name(name: str, obj: object) -> None:
            names.append(name)
            real_add(name=name, obj=obj)

        monkeypatch.setattr(object_manager, "add_object_by_name", add_object_by_name)
        return names

    @staticmethod
    def _flow_names(engine: Engine) -> set[str]:
        return set(engine.object_manager.get_filtered_subset(type=ControlFlow))

    @staticmethod
    def _node_names(engine: Engine) -> set[str]:
        return set(engine.object_manager.get_filtered_subset(type=BaseNode))

    async def _failing_import(
        self, tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], *, grouped: bool
    ) -> tuple[str, str]:
        """Register a failing workflow and a parent flow; return the workflow name and the parent flow name."""
        library_json = materialize_library(
            tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
        )
        workflow_path = tmp_path / "failing_inner_workflow.py"
        _write_grouped_workflow_file(engine, library_json, workflow_path, grouped=grouped, fails=True)
        registered = await engine.ahandle_request(ImportWorkflowRequest(file_path=str(workflow_path)))
        assert isinstance(registered, ImportWorkflowResultSuccess), registered

        engine.context_manager.push_workflow(workflow_name="parent_workflow")
        parent_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
        )
        assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
        return registered.workflow_name, parent_result.flow_name

    async def _failed_import_built(
        self, engine: Engine, created_names: list[str], workflow_name: str, parent_flow: str
    ) -> list[str]:
        """Run the import, assert it failed after building things, and return the names it built."""
        start = len(created_names)

        result = await engine.ahandle_request(
            ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=parent_flow)
        )

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure), result
        assert "the workflow file failed" in str(result.result_details), result
        built = created_names[start:]
        # The file must have got as far as building its graph, or the cleanup checks below prove nothing.
        assert "Echo_1" in built, built
        return built

    @pytest.mark.asyncio
    async def test_failed_import_leaves_no_flows_or_nodes(
        self,
        tmp_path: Path,
        engine: Engine,
        materialize_library: Callable[..., Path],
        created_names: list[str],
    ) -> None:
        """Nothing the failed import built stays in the workflow."""
        workflow_name, parent_flow = await self._failing_import(tmp_path, engine, materialize_library, grouped=False)

        await self._failed_import_built(engine, created_names, workflow_name, parent_flow)

        assert self._flow_names(engine) == {parent_flow}
        assert self._node_names(engine) == set()

    @pytest.mark.asyncio
    async def test_failed_import_is_not_serialized(
        self,
        tmp_path: Path,
        engine: Engine,
        materialize_library: Callable[..., Path],
        created_names: list[str],
    ) -> None:
        """Saving the parent after a failed import writes no import of the failed workflow."""
        workflow_name, parent_flow = await self._failing_import(tmp_path, engine, materialize_library, grouped=False)
        await self._failed_import_built(engine, created_names, workflow_name, parent_flow)

        serialize_result = engine.handle_request(
            SerializeFlowToCommandsRequest(flow_name=parent_flow, include_create_flow_command=True)
        )

        assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result
        commands = serialize_result.serialized_flow_commands
        assert not any(
            isinstance(sub_flow.flow_initialization_command, ImportWorkflowAsReferencedSubFlowRequest)
            for sub_flow in commands.sub_flows_commands
        ), commands
        assert self._flow_names(engine) == {parent_flow}

    @pytest.mark.asyncio
    async def test_failed_import_of_group_workflow_removes_group_body_flow(
        self,
        tmp_path: Path,
        engine: Engine,
        materialize_library: Callable[..., Path],
        created_names: list[str],
    ) -> None:
        """The flow inside a node group goes too, along with the group's nodes."""
        workflow_name, parent_flow = await self._failing_import(tmp_path, engine, materialize_library, grouped=True)

        built = await self._failed_import_built(engine, created_names, workflow_name, parent_flow)

        assert "SubflowGroup_1" in built, built
        assert self._flow_names(engine) == {parent_flow}
        assert self._node_names(engine) == set()

    @pytest.mark.asyncio
    async def test_retry_after_failed_import_reuses_the_same_names(
        self,
        tmp_path: Path,
        engine: Engine,
        materialize_library: Callable[..., Path],
        created_names: list[str],
    ) -> None:
        """A second attempt builds flows and nodes with the same names as the first, not numbered copies."""
        workflow_name, parent_flow = await self._failing_import(tmp_path, engine, materialize_library, grouped=True)

        first = await self._failed_import_built(engine, created_names, workflow_name, parent_flow)
        second = await self._failed_import_built(engine, created_names, workflow_name, parent_flow)

        assert second == first
