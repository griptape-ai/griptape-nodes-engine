"""A group removes a proxy parameter it made once no connection runs through it, and not before."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, DeleteConnectionRequest
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY = "proxy-cleanup-library"


class _Pass(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(Parameter(name="value", type="str", default_value="", tooltip=""))
        self.add_parameter(
            Parameter(name="result", type="str", default_value=None, tooltip="", allowed_modes={ParameterMode.OUTPUT})
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = self.get_parameter_value("value")


class _Group(SubflowNodeGroup):
    async def aprocess(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _library() -> Iterator[None]:
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(_Pass, NodeMetadata(category="t", description="d", display_name="Pass"))
    library.register_new_node_type(_Group, NodeMetadata(category="t", description="d", display_name="Group"))
    yield
    LibraryRegistry._clear()


@pytest.fixture
def workflow(engine: Engine) -> Iterator[WorkflowRuntime]:
    """A fresh open workflow with an empty flow, cleared after the test."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="proxies"))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    yield engine.open_workflows.get(result.workflow_id)
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _ok(workflow: WorkflowRuntime, request: RequestPayload) -> None:
    result = workflow.handle_request(request)
    assert result.succeeded(), result.result_details


def _connections(workflow: WorkflowRuntime) -> set[tuple[str, str, str, str]]:
    return {
        (c.source_node.name, c.source_parameter.name, c.target_node.name, c.target_parameter.name)
        for c in workflow.flows.connections.connections.values()
    }


@pytest.fixture
def grouped(workflow: WorkflowRuntime) -> WorkflowRuntime:
    """``outside`` feeds ``inside``, which a group holds, through the group's ``result`` proxy."""
    for name in ("outside", "inside"):
        _ok(workflow, CreateNodeRequest(node_type="_Pass", specific_library_name=_LIBRARY, node_name=name))
    _ok(
        workflow,
        CreateConnectionRequest(
            source_node_name="outside",
            source_parameter_name="result",
            target_node_name="inside",
            target_parameter_name="value",
        ),
    )
    _ok(
        workflow,
        CreateNodeRequest(
            node_type="_Group", specific_library_name=_LIBRARY, node_name="group", node_names_to_add=["inside"]
        ),
    )
    assert _connections(workflow) == {("outside", "result", "group", "result"), ("group", "result", "inside", "value")}
    return workflow


def _outside_to_proxy(*, delete: bool) -> RequestPayload:
    request_type = DeleteConnectionRequest if delete else CreateConnectionRequest
    return request_type(
        source_node_name="outside",
        source_parameter_name="result",
        target_node_name="group",
        target_parameter_name="result",
    )


def test_a_proxy_reconnected_from_outside_survives_losing_that_connection_again(grouped: WorkflowRuntime) -> None:
    """The connection inside the group still runs through the proxy, so the proxy stays."""
    _ok(grouped, _outside_to_proxy(delete=True))
    _ok(grouped, _outside_to_proxy(delete=False))

    _ok(grouped, _outside_to_proxy(delete=True))

    assert _connections(grouped) == {("group", "result", "inside", "value")}


def test_a_proxy_goes_once_nothing_connects_through_it(grouped: WorkflowRuntime) -> None:
    """A proxy with no connection left serves nothing."""
    _ok(grouped, _outside_to_proxy(delete=True))
    _ok(
        grouped,
        DeleteConnectionRequest(
            source_node_name="group",
            source_parameter_name="result",
            target_node_name="inside",
            target_parameter_name="value",
        ),
    )

    group = grouped.objects.get_as_type("group", _Group)
    assert group is not None
    assert group.get_parameter_by_name("result") is None
