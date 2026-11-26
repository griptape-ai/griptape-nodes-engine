"""The wire shape of an execution event, and of the progress event the app turns into one.

`data/execution_event_golden.json` is shared: the app suite copies it. A `ProgressEvent` with the
golden's fields must become the golden `ExecutionEvent` on the wire. This suite pins the engine's
side, the app's suite pins its conversion, so renaming a field fails in both repos.
"""

import dataclasses
import json
from pathlib import Path

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ProgressEvent
from griptape_nodes.retained_mode.events.execution_events import GriptapeEvent, progress_to_execution_event

GOLDEN_PATH = Path(__file__).parent / "data" / "execution_event_golden.json"


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_progress_event_fields_match_the_golden() -> None:
    """The app builds a progress event from these fields, so a renamed field fails here."""
    progress = ProgressEvent(**_golden()["ProgressEvent"])

    assert dataclasses.asdict(progress) == _golden()["ProgressEvent"]


def test_an_execution_event_serializes_to_the_golden() -> None:
    """A progress event converted the way the engine converts it reaches the wire as the golden."""
    progress = ProgressEvent(**_golden()["ProgressEvent"])

    event = progress_to_execution_event(progress)

    assert json.loads(converters.engine.dumps(event)) == _golden()["ExecutionEvent"]


def test_the_golden_execution_event_deserializes() -> None:
    """Clients that read the golden back get its workflow id."""
    event = converters.engine.structure(_golden()["ExecutionEvent"], ExecutionEvent)

    assert event.open_workflow_id == _golden()["ExecutionEvent"]["open_workflow_id"]
    assert isinstance(event.payload, GriptapeEvent)
