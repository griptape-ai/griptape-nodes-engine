"""Regression test for issue #5665: a workflow that imports itself as a referenced sub-flow.

A Workflow node can end up pointing at the workflow that contains it (directly, or through a
template). Importing that workflow runs its file, whose import of itself runs the file again, and
so on until the engine dies. The import must refuse a workflow that is already being imported
somewhere above the target flow, while still allowing the same workflow to be imported twice side
by side. The saved ``workflows_referenced`` metadata lets the outermost import refuse up front; when
that metadata is stale, the import nested inside the cycle refuses instead, so the recursion stops
after one level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultSuccess,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(120, method="thread")

_WORKFLOW_TEMPLATE = """\
# /// script
# dependencies = []
#
# [tool.griptape-nodes]
# name = "{name}"
# schema_version = "{schema_version}"
# engine_version_created_with = "0.0.0"
# node_libraries_referenced = []
# workflows_referenced = [{referenced}]
# is_griptape_provided = false
# is_internal = false
# creation_date = 2026-01-01T00:00:00Z
# last_modified_date = 2026-01-01T00:00:00Z
#
# ///

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


async def build_workflow() -> None:
    flow0_name = (
        await GriptapeNodes.ahandle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False, metadata={{}})
        )
    ).flow_name
    with GriptapeNodes.ContextManager().flow(flow0_name):
{imports}
        pass


async def main() -> None:
    await build_workflow()
"""


def _write_workflow(path: Path, *, imports: list[str]) -> None:
    import_lines = "".join(
        f"        await GriptapeNodes.ahandle_request(ImportWorkflowAsReferencedSubFlowRequest("
        f'workflow_name="{name}", imported_flow_metadata={{}}))\n'
        for name in imports
    )
    referenced = ", ".join(f'"{name}"' for name in sorted(set(imports)))
    path.write_text(
        _WORKFLOW_TEMPLATE.format(
            name=path.stem,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            referenced=referenced,
            imports=import_lines,
        )
    )


async def _register(engine: Engine, path: Path) -> str:
    result = await engine.ahandle_request(ImportWorkflowRequest(file_path=str(path)))
    assert isinstance(result, ImportWorkflowResultSuccess), result
    return result.workflow_name


def _parent_flow(engine: Engine) -> str:
    engine.context_manager.push_workflow(workflow_name="parent_workflow")
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _flow_count(engine: Engine) -> int:
    return len(engine.object_manager.get_filtered_subset(type=ControlFlow))


@pytest.mark.asyncio
async def test_import_of_self_referencing_workflow_is_refused(tmp_path: Path, engine: Engine) -> None:
    """A workflow whose saved references name itself is refused before anything is created."""
    workflow_path = tmp_path / "self_ref.py"
    _write_workflow(workflow_path, imports=[derive_registry_key(str(workflow_path))])
    workflow_name = await _register(engine, workflow_path)

    parent_flow = _parent_flow(engine)
    result = await engine.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=parent_flow)
    )

    assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure), result
    assert _flow_count(engine) == 1


@pytest.mark.asyncio
async def test_import_of_mutually_referencing_workflows_is_refused(tmp_path: Path, engine: Engine) -> None:
    """Two workflows that import each other are refused before anything is created."""
    path_a = tmp_path / "cycle_a.py"
    path_b = tmp_path / "cycle_b.py"
    _write_workflow(path_a, imports=[derive_registry_key(str(path_b))])
    _write_workflow(path_b, imports=[derive_registry_key(str(path_a))])
    name_a = await _register(engine, path_a)
    await _register(engine, path_b)

    parent_flow = _parent_flow(engine)
    result = await engine.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=name_a, flow_name=parent_flow)
    )

    assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure), result
    assert _flow_count(engine) == 1


@pytest.mark.asyncio
async def test_self_import_with_stale_references_stops_after_one_level(tmp_path: Path, engine: Engine) -> None:
    """A self-import the saved references miss is refused one level down, by flow ancestry."""
    workflow_path = tmp_path / "stale_self_ref.py"
    _write_workflow(workflow_path, imports=[])
    workflow_name = await _register(engine, workflow_path)
    # The registry still holds the metadata read above, which references nothing.
    _write_workflow(workflow_path, imports=[workflow_name])

    parent_flow = _parent_flow(engine)
    await engine.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=parent_flow)
    )

    # The parent flow plus the one imported copy whose own self-import was refused.
    assert _flow_count(engine) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_same_workflow_imported_twice_side_by_side_is_allowed(tmp_path: Path, engine: Engine) -> None:
    """Importing one workflow twice into the same flow is not a cycle."""
    inner_path = tmp_path / "leaf.py"
    outer_path = tmp_path / "outer.py"
    inner_name = derive_registry_key(str(inner_path))
    _write_workflow(inner_path, imports=[])
    _write_workflow(outer_path, imports=[inner_name, inner_name])
    await _register(engine, inner_path)
    outer_name = await _register(engine, outer_path)

    parent_flow = _parent_flow(engine)
    result = await engine.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=outer_name, flow_name=parent_flow)
    )

    assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result
    # The parent, the outer workflow's flow, and one flow per inner import.
    assert _flow_count(engine) == 4  # noqa: PLR2004
