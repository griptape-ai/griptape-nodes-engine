"""End-to-end coverage for ``NodeErrorEvent.error``, driven by the documented example node.

The node under test is ``docs/development/custom_nodes/example_node_error_node.py``, loaded as-is
into a fixture library, so the example authors copy is the code these tests run. Each test resolves
the node through a real engine and reads the ``NodeErrorEvent`` the editor would receive.

The worker path is covered by sending the real failure result through the same converter a worker
uses, in ``tests/unit/common/test_node_errors.py``. Spawning a worker here would need an offline
virtual environment and adds nothing the converter round trip does not already prove.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.execution_events import NodeErrorEvent, ResolveNodeRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(120, method="thread")

REPO_ROOT = Path(__file__).parents[2]
EXAMPLE_NODE_FILE = REPO_ROOT / "docs" / "development" / "custom_nodes" / "example_node_error_node.py"
FIXTURE_LIBRARY_JSON_TEMPLATE = (
    Path(__file__).parent / "fixtures" / "node_error_library" / "griptape_nodes_library.json"
)
LIBRARY_NAME = "Node Error Library"
NODE_TYPE = "ExampleNodeErrorNode"
NODE_NAME = "NodeError Example"


@pytest.fixture
def registered_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    """Register a fixture library whose only node is the docs example, copied unchanged."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=EXAMPLE_NODE_FILE
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def _record_node_errors(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[NodeErrorEvent]:
    """Tap the async event bus, which is where the resolution machine publishes node errors."""
    recorded: list[NodeErrorEvent] = []
    event_manager = engine.event_manager
    real_aput_event = event_manager.aput_event

    async def _collect(event: Any) -> None:
        payload = getattr(getattr(event, "wrapped_event", None), "payload", event)
        if isinstance(payload, NodeErrorEvent):
            recorded.append(payload)
        await real_aput_event(event)

    monkeypatch.setattr(event_manager, "aput_event", _collect)
    return recorded


async def _run_with_failure(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str], failure: str
) -> NodeErrorEvent:
    engine.context_manager.push_workflow(workflow_name=f"structured_errors_{failure}")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    create_node(NODE_TYPE, NODE_NAME, flow.flow_name, library_name=LIBRARY_NAME)
    engine.handle_request(SetParameterValueRequest(parameter_name="failure", node_name=NODE_NAME, value=failure))
    recorded = _record_node_errors(engine, monkeypatch)

    await engine.ahandle_request(ResolveNodeRequest(node_name=NODE_NAME))

    assert len(recorded) == 1, recorded
    return recorded[0]


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_node_error_arrives_with_its_fields_response_and_link(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """A NodeError keeps its parts, and error_message keeps the old flattened text."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Provider failure")

    assert event.error is not None
    assert event.error.message == "The image service could not process the request: proxy client error."
    assert event.error.exception_type == "griptape_nodes.exe_types.node_error.NodeError"
    assert event.error.fields == {"generation_id": "90dbcfa0-ac4b-4feb-b234-0badff151ee2", "status": "ERRORED"}
    assert event.error.response is not None
    assert event.error.response["status_detail"]["details"] == "proxy client error"
    assert [link.label for link in event.error.links] == ["Error handling guide"]
    assert "Attempted to execute node" in event.error_message


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_wrapped_http_error_arrives_with_status_request_id_and_body(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """An SDK exception re-raised as a NodeError keeps the provider's explanation and its identifiers."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Service rejected the request")

    assert event.error is not None
    assert event.error.message == (
        "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."
    )
    assert event.error.fields == {
        "status_code": "400",
        "error_code": "unsupported_parameter",
        "parameter": "max_tokens",
        "request_id": "req_8f2c41d07a9e4b15",
    }
    assert event.error.response is not None
    assert event.error.response["error"]["type"] == "invalid_request_error"


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_missing_api_key_is_caught_before_the_node_runs(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """The secrets lookup finds nothing in the isolated engine, so validation reports it with a link."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Missing API key")

    assert event.error is not None
    expected = "EXAMPLE_SERVICE_API_KEY is not set. Add it in Settings → API Keys & Secrets, then run the node again."
    assert event.error.messages == [expected]
    assert event.error.exception_type == "griptape_nodes.exe_types.node_error.NodeError"
    assert [link.label for link in event.error.links] == ["Editing settings"]


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_links_arrive_in_order_without_fields_or_response(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """A NodeError that only attaches links sends them as given, with nothing else attached."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Unsupported format")

    assert event.error is not None
    assert event.error.message.startswith("Image format 'image/heic' is not supported.")
    assert [(link.label, link.url) for link in event.error.links] == [
        ("Supported image formats", "https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html"),
        ("Error handling guide", "https://docs.griptapenodes.com/development/custom_nodes/error_handling/"),
    ]
    assert event.error.fields == {}
    assert event.error.response is None


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_key_error_arrives_without_quotes_and_with_its_type(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """A plain KeyError arrives without the quotes Python adds, and with its type."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Missing preset setting")

    assert event.error is not None
    assert event.error.message == "The preset has no 'strength' setting. Pick another preset."
    assert event.error.exception_type == "builtins.KeyError"
    assert event.error.fields == {}
    assert event.error.response is None


@pytest.mark.usefixtures("registered_library")
@pytest.mark.asyncio
async def test_validation_problems_arrive_one_per_line(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
) -> None:
    """Exceptions from validate_before_node_run arrive as one message each, not a list repr."""
    event = await _run_with_failure(engine, monkeypatch, create_node, "Validation problems")

    assert event.error is not None
    assert event.error.messages == [
        "Connect an image to 'Input Image'.",
        "'Prompt' is empty. Describe the edit you want.",
    ]
    assert "ValueError(" in event.error_message
