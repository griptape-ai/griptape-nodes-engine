"""End-to-end coverage for usage reports sent by an engine that exits right after its run (issue #5958).

A direct provider call's cost is reported to Griptape Cloud from a background thread, which dies with
the process. A one-off engine, such as a workflow run from the command line, exits as soon as its run
ends, so it has to wait for that thread first. This runs a real one-off engine against a stub Cloud
that holds each reply, so a report is still in flight when the run ends.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from griptape_nodes.drivers.cloud_credentials import BASE_URL_SETTING_NAME
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

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "usage_report_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "usage_report_nodes.py"
LIBRARY_NAME = "Usage Report Library"
NODE_TYPE = "ReportingNode"

SPEND_PATH = "/api/spend"
# Long enough that the report is still in flight when the one-off engine's run ends.
REPLY_DELAY_SECONDS = 1.0


@pytest.fixture
def stub_cloud() -> Iterator[_StubSpendServer]:
    """Run a stub Griptape Cloud that records usage reports."""
    server = _StubSpendServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _StubSpendServer(ThreadingHTTPServer):
    """Records every usage report posted to it, holding each reply for `reply_delay` seconds."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StubSpendHandler)
        self.reports: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.reply_delay = 0.0


class _StubSpendHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        server = cast("_StubSpendServer", self.server)
        time.sleep(server.reply_delay)
        if self.path == SPEND_PATH:
            with server.lock:
                server.reports.append(body)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # the base class names it this
        """Swallow the per-request line the base class writes to stderr."""


def _runnable_workflow_source(engine: Engine, flow_name: str) -> str:
    """Serialize the flow to a workflow file that runs itself once, the way a one-off engine does."""
    serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized
    metadata = WorkflowMetadata(
        name="usage_report_exit_wf",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialized.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=None,
    )
    source = engine.workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialized.serialized_flow_commands,
        workflow_metadata=metadata,
    )
    return (
        source
        + """

import asyncio as _e2e_asyncio

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    LocalWorkflowExecutor as _E2ELocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage.storage_backend import StorageBackend as _E2EStorageBackend
from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest as _E2EGetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess as _E2EGetTopLevelFlowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes as _E2EGriptapeNodes


async def _e2e_run() -> None:
    await build_workflow()  # noqa: F821 - defined by the generated workflow source above
    context_manager = _E2EGriptapeNodes.ContextManager()
    if not context_manager.has_current_flow():
        top_level = _E2EGriptapeNodes.handle_request(_E2EGetTopLevelFlowRequest())
        if isinstance(top_level, _E2EGetTopLevelFlowResultSuccess) and top_level.flow_name is not None:
            context_manager.push_flow(_E2EGriptapeNodes.FlowManager().get_flow_by_name(top_level.flow_name))
    executor = _E2ELocalWorkflowExecutor(
        storage_backend=_E2EStorageBackend.LOCAL, skip_library_loading=True, workflows_to_register=[__file__]
    )
    async with executor as running:
        await running.arun(flow_input={})


if __name__ == "__main__":
    _e2e_asyncio.run(_e2e_run())
"""
    )


def test_an_engine_that_exits_after_its_run_still_sends_its_reports(  # noqa: PLR0913
    tmp_path: Path,
    engine: Engine,
    stub_cloud: _StubSpendServer,
    *,
    engine_subprocess_env: Callable[..., dict[str, str]],
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
    create_node: Callable[..., str],
) -> None:
    """A one-off engine reports a call in its last node, then exits as soon as the run ends.

    The report thread is a daemon, so without waiting for it the process ends with the report
    unsent. Cloud's reply is held long enough that the report is still in flight at that point.
    """
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered
    engine.context_manager.push_workflow(workflow_name="usage_report_exit_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow"))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    create_node(NODE_TYPE, "Reporter", flow.flow_name, library_name=LIBRARY_NAME)
    workflow_path = tmp_path / "usage_report_exit_wf.py"
    workflow_path.write_text(_runnable_workflow_source(engine, flow.flow_name))

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config_root = tmp_path / "xdg_config"
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)
    host, port = stub_cloud.server_address[0], stub_cloud.server_address[1]
    stub_cloud.reply_delay = REPLY_DELAY_SECONDS

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(workflow_path)],
        env=engine_subprocess_env(XDG_CONFIG_HOME=str(config_root), **{BASE_URL_SETTING_NAME: f"http://{host}:{port}"}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    diagnostic = f"exit code {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    assert result.returncode == 0, diagnostic
    with stub_cloud.lock:
        reports = list(stub_cloud.reports)
    assert len(reports) == 1, f"The engine exited without sending its report to Cloud.\n{diagnostic}"
