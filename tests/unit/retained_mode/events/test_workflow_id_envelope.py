"""The open workflow's id travels on the request envelope, never in the request body.

`data/workflow_id_envelope_golden.json` is shared: the editor and app suites copy it. The editor
asserts it emits this shape, the app that it forwards the id, and this suite that the engine
reads it and echoes it on the result.
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.retained_mode.engine import Engine, current_engine
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventRequestBatch,
    EventResultSuccess,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.context_events import GetWorkflowContextRequest, GetWorkflowContextSuccess
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

GOLDEN_PATH = Path(__file__).parent / "data" / "workflow_id_envelope_golden.json"
GOLDEN_WORKFLOW_ID = "5b0f6c3e8d1a4f7e9c2b6a4d3e1f0b97"


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _open_golden_workflow(engine: Engine) -> None:
    engine.workflow_registry.register_unsaved("Golden", workflow_id=GOLDEN_WORKFLOW_ID)
    engine.context_manager.push_workflow(workflow_name=f"unsaved:{GOLDEN_WORKFLOW_ID}")


def test_the_engine_reads_the_envelope_workflow_id() -> None:
    """The id sits beside `request_type`, not inside the request."""
    event = converters.engine.structure(_golden()["EventRequest"], EventRequest)

    assert event.workflow_id == GOLDEN_WORKFLOW_ID
    assert isinstance(event.request, GetWorkflowContextRequest)
    assert "workflow_id" not in _golden()["EventRequest"]["request"]


def test_each_request_in_a_batch_carries_its_own_workflow_id() -> None:
    """A batch has no envelope id of its own; each inner request names its workflow."""
    batch = converters.engine.structure(_golden()["EventRequestBatch"], EventRequestBatch)

    assert [inner.workflow_id for inner in batch.requests] == [GOLDEN_WORKFLOW_ID]


def test_the_envelope_workflow_id_round_trips() -> None:
    """Serializing the envelope keeps the id at envelope level."""
    event = converters.engine.structure(_golden()["EventRequest"], EventRequest)

    wire = json.loads(converters.engine.dumps(event))

    assert wire["workflow_id"] == GOLDEN_WORKFLOW_ID
    assert "workflow_id" not in wire["request"]


@pytest.mark.asyncio
async def test_the_result_echoes_the_envelope_workflow_id() -> None:
    """The request runs in the workflow the id names, and its result carries the id back."""
    engine = current_engine()
    _open_golden_workflow(engine)
    event = converters.engine.structure(_golden()["EventRequest"], EventRequest)
    result_context = ResultContext(request_id=event.request_id, response_topic=event.response_topic)

    result_event = await engine.event_manager.ahandle_request(
        event.request, result_context=result_context, workflow_id=event.workflow_id
    )

    assert isinstance(result_event.result, GetWorkflowContextSuccess)
    assert result_event.workflow_id == GOLDEN_WORKFLOW_ID
    assert result_event.request_id == "golden-request-1"
    assert json.loads(converters.engine.dumps(result_event))["workflow_id"] == GOLDEN_WORKFLOW_ID


def test_the_sync_path_echoes_the_envelope_workflow_id() -> None:
    """`handle_request` takes the envelope id the same way `ahandle_request` does."""
    engine = current_engine()
    _open_golden_workflow(engine)
    result_event = engine.event_manager.handle_request(GetWorkflowContextRequest(), workflow_id=GOLDEN_WORKFLOW_ID)

    assert result_event.succeeded()
    assert result_event.workflow_id == GOLDEN_WORKFLOW_ID


@dataclasses.dataclass
class _ResultHoldingAnything(ResultPayloadSuccess):
    anything: Any = None


def test_a_result_that_cannot_be_sent_still_echoes_the_envelope_workflow_id() -> None:
    """The failure sent in its place names the same workflow."""
    event = EventResultSuccess(
        request=GetWorkflowContextRequest(),
        result=_ResultHoldingAnything(result_details="ok", anything=object()),
        workflow_id=GOLDEN_WORKFLOW_ID,
    )

    result_json = converters.engine.dumps_result(event)

    assert result_json.error is not None
    assert json.loads(result_json.text)["workflow_id"] == GOLDEN_WORKFLOW_ID
