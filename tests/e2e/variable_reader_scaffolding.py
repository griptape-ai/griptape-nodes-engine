"""Shared scaffolding for the e2e tests that check when a node reading a variable runs again.

They build small graphs from the ``variable_reader_library`` fixture in workflows opened beside
each other, as editor tabs are, and send every request with that workflow named on the envelope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.project_templates import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.context_events import (
    CloseWorkflowRequest,
    OpenWorkflowRequest,
    OpenWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ResolveNodeRequest,
    ResolveNodeResultSuccess,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
    SetCurrentProjectResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    SetVariableValueRequest,
    SetVariableValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import SaveWorkflowRequest, SaveWorkflowResultSuccess
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "variable_reader_library"
LIBRARY_NAME = "Variable Reader Library"
FLOW = "canvas"
_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01


class ReaderTab:
    """One open workflow, as an editor tab, and the requests these tests send to it."""

    def __init__(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        self.engine = engine
        self.workflow = workflow

    def send(self, request: RequestPayload) -> ResultPayload:
        return self.engine.event_manager.handle_request(
            request, result_context=ResultContext(open_workflow_id=self.workflow.workflow_id)
        ).result

    async def send_async(self, request: RequestPayload) -> ResultPayload:
        envelope = await self.engine.event_manager.ahandle_request(
            request, result_context=ResultContext(open_workflow_id=self.workflow.workflow_id)
        )
        return envelope.result

    def add_node(self, node_type: str, name: str, *, text: str = "", library: str = LIBRARY_NAME) -> None:
        result = self.send(
            CreateNodeRequest(
                node_type=node_type, specific_library_name=library, node_name=name, override_parent_flow_name=FLOW
            )
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        if text:
            self.set_parameter(name, "text", text)

    def set_parameter(self, node: str, parameter: str, value: str) -> None:
        result = self.send(SetParameterValueRequest(node_name=node, parameter_name=parameter, value=value))
        assert result.succeeded(), result

    def connect(self, source: str, target: str, target_parameter: str, *, source_parameter: str = "result") -> None:
        result = self.send(
            CreateConnectionRequest(
                source_node_name=source,
                source_parameter_name=source_parameter,
                target_node_name=target,
                target_parameter_name=target_parameter,
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result

    def create_variable(self, name: str, value: str) -> None:
        result = self.send(CreateVariableRequest(name=name, type="str", value=value, owning_flow=FLOW))
        assert isinstance(result, CreateVariableResultSuccess), result

    def set_variable(self, name: str, value: str) -> None:
        result = self.send(SetVariableValueRequest(name=name, value=value, starting_flow=FLOW))
        assert isinstance(result, SetVariableValueResultSuccess), result

    async def play(self, node: str) -> None:
        """Press play on one node, which always runs that node and whatever it needs."""
        result = await self.send_async(ResolveNodeRequest(node_name=node))
        assert isinstance(result, ResolveNodeResultSuccess), result

    async def run(self) -> None:
        """Press Run, which runs the whole workflow along its control chains."""
        result = await self.send_async(StartFlowRequest(flow_name=FLOW, flow_node_name=None))
        assert isinstance(result, StartFlowResultSuccess), result

    def node(self, name: str) -> BaseNode:
        node = self.workflow.objects.get_as_type(name, BaseNode)
        assert node is not None, f"No node named '{name}' in this workflow."
        return node

    def output(self, node: str, parameter: str) -> object:
        return self.node(node).parameter_output_values.get(parameter)

    def runs(self, node: str) -> object:
        return self.output(node, "run_count")

    def state(self, node: str) -> NodeResolutionState:
        return self.node(node).state

    async def wait_until_resolving(self, node: str) -> None:
        for _ in range(_POLL_ATTEMPTS):
            if self.state(node) is NodeResolutionState.RESOLVING:
                return
            await asyncio.sleep(_POLL_SECONDS)
        pytest.fail(f"Node '{node}' never started running.")

    def save(self, file_name: str) -> str:
        """Save this workflow to a file and return its registry name."""
        result = self.send(SaveWorkflowRequest(file_name=file_name))
        assert isinstance(result, SaveWorkflowResultSuccess), result
        return result.workflow_name

    def close(self) -> None:
        result = self.engine.handle_request(CloseWorkflowRequest(workflow_id=self.workflow.workflow_id))
        assert result.succeeded(), result

    @classmethod
    def open_new(cls, engine: Engine, display_name: str) -> ReaderTab:
        """Open a new unsaved workflow beside whatever is open, with one flow."""
        workflow = engine.open_workflow(
            engine.workflow_registry.register_unsaved(display_name, listed=False).workflow_id
        )
        tab = cls(engine, workflow)
        result = tab.send(CreateFlowRequest(parent_flow_name=None, flow_name=FLOW))
        assert isinstance(result, CreateFlowResultSuccess), result
        return tab

    @classmethod
    async def open_saved(cls, engine: Engine, workflow_name: str) -> ReaderTab:
        """Open a saved workflow, which builds its graph from its file."""
        result = await engine.ahandle_request(OpenWorkflowRequest(workflow_name=workflow_name))
        assert isinstance(result, OpenWorkflowResultSuccess), result
        return cls(engine, engine.open_workflows.get(result.open_workflow.workflow_id))


def register_reader_library(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path) -> None:
    """Copy the reader fixture library into `tmp_path` and register it in `engine`."""
    library_json = materialize_library(
        tmp_path / "reader_lib",
        template=FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_LIBRARY_DIR / "variable_reader_nodes.py",
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def load_project(engine: Engine, directory: Path, shot_code: str) -> str:
    """Write and load a project whose writable `shot_code` variable holds `shot_code`, and return its id."""
    directory.mkdir(parents=True, exist_ok=True)
    project_yml = directory / "project_template.yml"
    project_yml.write_text(
        DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        + f"\nvariables:\n  shot_code:\n    value: {shot_code}\n"
    )
    result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(result, LoadProjectTemplateResultSuccess), result
    return result.project_id


async def switch_project(engine: Engine, project_id: str | None) -> None:
    """Make `project_id` current, or the system defaults for None, as switching projects in the editor does."""
    result = await engine.ahandle_request(SetCurrentProjectRequest(project_id=project_id))
    assert isinstance(result, SetCurrentProjectResultSuccess), result


def open_gate(gate_file: Path) -> None:
    """Create the gate file a waiting `VariableReader` polls for, which lets it finish."""
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text("open")
