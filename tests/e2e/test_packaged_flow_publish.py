"""A node group run through a library publisher reaches the publisher under the packaged flow's own name.

`NodeExecutor` packages the group to `{node}_{library}_packaged_flow.py` in the workspace and has
`SubprocessWorkflowPublisher` publish it from a real subprocess. Publishers name what they create
after `workflow_name` and look the workflow up with `WorkflowRegistry.get_workflow_by_name`, so the
name must be that file's registry name and must resolve to the workflow the subprocess loaded.

The subprocess normally reports progress over a Griptape Cloud WebSocket. The test runs without one:
the parent's listener is skipped and the subprocess is started without a session, so it publishes
with `LocalWorkflowPublisher` instead of `LocalSessionWorkflowPublisher`. Everything else is real.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import anyio
import pytest

from griptape_nodes.bootstrap.workflow_publishers.subprocess_workflow_publisher import SubprocessWorkflowPublisher
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.node_library.workflow_registry import read_workflow_metadata
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from tests.workflow_context import push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "publish_library"
LIBRARY_NAME = "Publish Library"


def _files_under(directory: Path) -> set[Path]:
    return {path.relative_to(directory) for path in directory.rglob("*") if path.is_file()}


@pytest.mark.asyncio
async def test_the_publisher_gets_the_packaged_flow_by_its_registry_name(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
) -> None:
    """The name the publisher receives resolves to the workflow it publishes, and nothing else lands in the workspace."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_LIBRARY_DIR / "publish_nodes.py",
        extra_files=[FIXTURE_LIBRARY_DIR / "publish_library_advanced.py"],
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    push_unsaved_workflow(engine, "packaged_flow_publish_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    node_result = engine.handle_request(
        CreateNodeRequest(
            node_type="PublishEchoNode",
            specific_library_name=LIBRARY_NAME,
            node_name="Echo",
            override_parent_flow_name=flow_result.flow_name,
        )
    )
    assert isinstance(node_result, CreateNodeResultSuccess), node_result
    node = engine.node_manager.get_node_by_name(node_result.node_name)

    # The subprocess reads the same workspace and loads the same library, from its own config.
    workspace = engine.config_manager.workspace_path
    config_root = tmp_path / "subprocess_config"
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)
    record_path = tmp_path / "publish_record.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("PUBLISH_FIXTURE_RECORD_PATH", str(record_path))
    # Engine bootstrap requires GT_CLOUD_API_KEY; the value never leaves the subprocess.
    monkeypatch.setenv("GT_CLOUD_API_KEY", "fake-test-key-for-bootstrap")

    node_executor = engine.flow_manager.node_executor
    with engine.context_manager.flow(flow_result.flow_name):
        packaged = await node_executor._publish_local_workflow(
            engine.open_workflows.sole_workflow(), node, library=LibraryRegistry.get_library(name=LIBRARY_NAME)
        )
    assert packaged is not None
    packaged_file = Path(packaged.workflow_result.file_path)
    packaged_workflow_id = read_workflow_metadata(packaged_file).workflow_id
    packaged_written_at = (await anyio.Path(packaged_file).stat()).st_mtime_ns
    assert packaged_file == workspace / "Echo_Publish_Library_packaged_flow.py"
    files_before_publish = _files_under(workspace)

    execute_python_script = SubprocessWorkflowPublisher.execute_python_script

    async def execute_without_session(self: SubprocessWorkflowPublisher, *, args: list[str], **kwargs: Any) -> None:
        session_index = args.index("--session-id")
        await execute_python_script(self, args=args[:session_index] + args[session_index + 2 :], **kwargs)

    async def no_listener(self: SubprocessWorkflowPublisher) -> None:
        pass

    with (
        patch.object(SubprocessWorkflowPublisher, "_start_websocket_listener", no_listener),
        patch.object(SubprocessWorkflowPublisher, "_stop_websocket_listener", no_listener),
        patch.object(SubprocessWorkflowPublisher, "execute_python_script", execute_without_session),
    ):
        published_file = await node_executor._publish_library_workflow(
            packaged.workflow_result, LIBRARY_NAME, node=node
        )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["workflow_name"] == "Echo_Publish_Library_packaged_flow"
    # The name resolves to the workflow the subprocess loaded and published: the packaged file, by its id.
    assert record["looked_up_workflow_id"] == record["workflow_id"] == record["open_workflow_id"]
    assert record["workflow_id"] == packaged_workflow_id
    # Saving before publishing rewrites the packaged file in place, under the id it already had.
    assert (await anyio.Path(packaged_file).stat()).st_mtime_ns != packaged_written_at
    assert read_workflow_metadata(packaged_file).workflow_id == packaged_workflow_id
    # The published file is the only file the publish adds to the workspace.
    assert published_file == workspace / "Echo_Publish_Library_packaged_flow_published.py"
    assert _files_under(workspace) - files_before_publish == {Path(published_file.name)}
