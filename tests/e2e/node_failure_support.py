"""Shared helpers for the node-failure golden tests.

The goldens run real flows through a real engine and record what each audience would receive: the
``NodeErrorEvent`` and ``NodeFinishProcessEvent`` the editor hears, the result a request returns, and
the ``converters.client`` form of that result, which is what MCP clients see.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventResultFailure, EventResultSuccess
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    NodeErrorEvent,
    NodeFinishProcessEvent,
    StartFlowRequest,
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from tests.simulated_worker import sent_from_open_workflow

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pytest

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails

FIXTURES_DIR = Path(__file__).parent / "fixtures"

WORKER_TRACEBACK_MARKER = "\nWorker traceback:\n"
TRACEBACK_HEADER = "Traceback (most recent call last):\n"
# Stands in for the frames of a traceback in a pinned string. Frames carry absolute paths and line numbers of
# the engine's own source, so a golden pins the header, the exception line and anything after, not the frames.
MASKED_FRAMES = "  <frames>"
FRAME_LINE_PREFIX = '  File "'
FRAME_SOURCE_PREFIXES = ("    ", "\t")
MASKED_RUN_ID = "<run id>"
# `ResultDetail.level` of a failure's details, which is `logging.ERROR`.
ERROR_LEVEL = 40
DEBUG_LEVEL = 10


@dataclass
class RecordedFailures:
    """The failure events a run published, in order."""

    node_errors: list[NodeErrorEvent] = field(default_factory=list)
    node_finishes: list[NodeFinishProcessEvent] = field(default_factory=list)

    def failed_finishes(self) -> list[NodeFinishProcessEvent]:
        """The finish events that carry an error."""
        return [finish for finish in self.node_finishes if finish.error_message is not None]

    def assert_finish_errors_match_node_errors(self, *, ordered: bool = True) -> None:
        """Assert each failed finish event carries the message and details of a ``NodeErrorEvent``, in order.

        ``ordered=False`` compares them sorted by message, for iterations that finish in any order.
        """
        errors = [(event.error_message, details_dict(event.error)) for event in self.node_errors]
        finishes = [(finish.error_message, details_dict(finish.error)) for finish in self.failed_finishes()]
        if not ordered:
            errors.sort(key=repr)
            finishes.sort(key=repr)
        assert finishes == errors


def record_failures(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> RecordedFailures:
    """Tap both event-bus entry points, since which one an event takes depends on who emits it."""
    recorded = RecordedFailures()
    event_manager = engine.event_manager
    real_put_event = event_manager.put_event
    real_aput_event = event_manager.aput_event

    def _note(event: object) -> None:
        payload = getattr(getattr(event, "wrapped_event", None), "payload", event)
        if isinstance(payload, NodeErrorEvent):
            recorded.node_errors.append(payload)
        elif isinstance(payload, NodeFinishProcessEvent):
            recorded.node_finishes.append(payload)

    def _collect(event: object, *args: Any, **kwargs: Any) -> None:
        _note(event)
        real_put_event(event, *args, **kwargs)

    async def _acollect(event: object, *args: Any, **kwargs: Any) -> None:
        _note(event)
        await real_aput_event(event, *args, **kwargs)

    monkeypatch.setattr(event_manager, "put_event", _collect)
    monkeypatch.setattr(event_manager, "aput_event", _acollect)
    return recorded


@dataclass
class FlowRun:
    """What a ``StartFlowRequest`` answered, and what the editor heard while it ran."""

    request: StartFlowRequest
    result: ResultPayload
    recorded: RecordedFailures
    run_message: str | None
    """The failure the run's resolution machine recorded, or None when it recorded none."""
    subflow_runs: list[RecordedSubflowRuns]
    """Every local subflow the run started, which is how groups, loops and Workflow nodes run their bodies."""

    @property
    def result_details(self) -> str:
        """The result's details, joined as the log and MCP show them."""
        return str(self.result.result_details)

    @property
    def validation_exception_messages(self) -> list[str]:
        """The message of each exception the result carries."""
        return [str(exception) for exception in getattr(self.result, "validation_exceptions", None) or []]

    @property
    def client(self) -> dict[str, Any]:
        """The ``converters.client`` form of the result, with tracebacks masked."""
        return masked_client_form(client_result(self.request, self.result))


class RecordedSubflowRuns(NamedTuple):
    """A ``StartLocalSubflowRequest`` a run sent, and the result it got."""

    request: StartLocalSubflowRequest
    result: ResultPayload


class ExecutedNode(NamedTuple):
    """An ``ExecuteNodeRequest`` and the failure it got."""

    request: ExecuteNodeRequest
    result: ExecuteNodeResultFailure


class MaskedTraceback(NamedTuple):
    """A message with the frames of its worker traceback masked, and the traceback that was there."""

    text: str
    traceback: str | None


async def start_flow(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    flow_name: str,
    *,
    flow_node_name: str | None = None,
    within_flow_context: bool = False,
) -> FlowRun:
    """Run the flow and gather what each audience was told about its failure.

    ``within_flow_context`` runs it with the flow current, which loop packaging needs.
    """
    recorded = record_failures(engine, monkeypatch)
    subflow_runs = record_subflow_runs(engine, monkeypatch)
    request = StartFlowRequest(flow_name=flow_name, flow_node_name=flow_node_name)
    if within_flow_context:
        with engine.context_manager.flow(flow_name):
            result = await engine.ahandle_request(request)
    else:
        result = await engine.ahandle_request(request)
    return FlowRun(
        request=request,
        result=result,
        recorded=recorded,
        run_message=recorded_run_message(engine),
        subflow_runs=subflow_runs,
    )


def recorded_run_message(engine: Engine) -> str | None:
    """The failure message the open workflow's resolution machine recorded, if a run left one."""
    machine = engine.current_workflow().flows.global_control_flow_machine
    if machine is None:
        return None
    return machine.resolution_machine.get_error_message()


def mask_worker_traceback(text: str) -> MaskedTraceback:
    """Mask the frames of the worker traceback a failure message embeds.

    The header, the exception line and any text after the traceback stay, so a golden pins them.
    """
    marker_index = text.find(WORKER_TRACEBACK_MARKER)
    if marker_index == -1:
        return MaskedTraceback(text=text, traceback=None)
    traceback_start = marker_index + len(WORKER_TRACEBACK_MARKER)
    traceback = text[traceback_start:]
    return MaskedTraceback(text=text[:traceback_start] + mask_frames(traceback), traceback=traceback)


def mask_frames(traceback: str) -> str:
    """``traceback`` with each run of frame lines replaced by one placeholder line.

    A frame is a ``  File "..."`` line and the more deeply indented source lines under it. Any other line,
    indented or not, such as the ``Traceback`` header or an exception message line, is kept.
    """
    masked: list[str] = []
    in_frame_run = False
    for line in traceback.split("\n"):
        if line.startswith(FRAME_LINE_PREFIX):
            if not in_frame_run:
                masked.append(MASKED_FRAMES)
            in_frame_run = True
        elif in_frame_run and line.startswith(FRAME_SOURCE_PREFIXES):
            continue
        else:
            in_frame_run = False
            masked.append(line)
    return "\n".join(masked)


def traceback_ending_with(last_line: str) -> str:
    """The masked form of a one-exception traceback whose exception line is ``last_line``."""
    return f"{TRACEBACK_HEADER}{MASKED_FRAMES}\n{last_line}\n"


def worker_traceback_block(last_line: str) -> str:
    """The masked form of the worker traceback block a message embeds, ending in ``last_line``."""
    return f"{WORKER_TRACEBACK_MARKER}{traceback_ending_with(last_line)}"


def masked_client_form(value: Any) -> Any:
    """``value`` with the frames of every traceback masked, after checking each reads as one.

    Covers the ``traceback`` field of a serialized exception and the worker traceback a message embeds.
    """
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if key == "traceback" and isinstance(item, str):
                assert item.startswith(TRACEBACK_HEADER), item
                masked[key] = mask_frames(item)
            elif key == "run_id" and isinstance(item, str):
                # A fresh id for every run.
                masked[key] = MASKED_RUN_ID
            else:
                masked[key] = masked_client_form(item)
        return masked
    if isinstance(value, list):
        return [masked_client_form(item) for item in value]
    if isinstance(value, str):
        return mask_worker_traceback(value).text
    return value


def expected_details(  # noqa: PLR0913
    message: str,
    exception_type: str | None,
    *,
    messages: list[str] | None = None,
    fields: dict[str, str] | None = None,
    response: dict[str, Any] | None = None,
    links: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A ``NodeErrorDetails`` as ``details_dict`` shows it, with every part present."""
    return {
        "message": message,
        "exception_type": exception_type,
        "messages": messages,
        "fields": fields or {},
        "response": response,
        "links": links or [],
    }


def client_exception(
    message: str, type_name: str = "builtins.RuntimeError", traceback: str | None = None
) -> dict[str, Any]:
    """A serialized exception as an MCP client receives it."""
    return {"type": type_name, "message": message, "traceback": traceback}


def client_run_failure(result_details: str, validation_exceptions: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The whole ``result`` object an MCP client receives for a failed ``StartFlowRequest`` or ``ResolveNodeRequest``."""
    return {
        "altered_workflow_state": False,
        "exception": None,
        "result_details": {"result_details": [{"level": ERROR_LEVEL, "message": result_details}]},
        "run_id": MASKED_RUN_ID,
        "validation_exceptions": validation_exceptions,
    }


def details_dict(details: NodeErrorDetails | None) -> dict[str, Any] | None:
    """A ``NodeErrorDetails`` as a plain dict, so a test pins every field, nested links included."""
    if details is None:
        return None
    return dataclasses.asdict(details)


def client_result(request: RequestPayload, result: ResultPayload) -> dict[str, Any]:
    """The ``result`` object an MCP client receives for ``request``, as ``servers.mcp`` builds it."""
    if result.succeeded():
        event: EventResultSuccess | EventResultFailure = EventResultSuccess(request=request, result=result)
    else:
        event = EventResultFailure(request=request, result=result)
    return json.loads(converters.client.dumps_result(event).text)["result"]


def register_fixture_library(  # noqa: PLR0913
    engine: Engine,
    target_dir: Path,
    materialize_library: Callable[..., Path],
    *,
    library_dir_name: str,
    node_file_name: str,
    name: str | None = None,
    extra_files: Sequence[Path] | None = None,
) -> None:
    """Materialize the fixture library ``library_dir_name`` under ``target_dir`` and register it."""
    library_dir = FIXTURES_DIR / library_dir_name
    library_json = materialize_library(
        target_dir,
        template=library_dir / "griptape_nodes_library.json",
        node_file=library_dir / node_file_name,
        name=name,
        extra_files=extra_files,
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def record_subflow_runs(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[RecordedSubflowRuns]:
    """Record every local subflow run, which is how groups, loops and Workflow nodes run their bodies.

    A group calls the flow manager directly, and everything else sends the request through the engine, so both
    entry points are tapped.
    """
    recorded: list[RecordedSubflowRuns] = []
    flow_manager = engine.flow_manager
    real_run = flow_manager.on_start_local_subflow_request
    real_ahandle_request = engine.ahandle_request

    async def _record_direct(request: StartLocalSubflowRequest, workflow: Any) -> ResultPayload:
        result = await real_run(request, workflow)
        recorded.append(RecordedSubflowRuns(request=request, result=result))
        return result

    async def _record_dispatched(request: RequestPayload, *args: Any, **kwargs: Any) -> ResultPayload:
        result = await real_ahandle_request(request, *args, **kwargs)
        if isinstance(request, StartLocalSubflowRequest):
            recorded.append(RecordedSubflowRuns(request=request, result=result))
        return result

    monkeypatch.setattr(flow_manager, "on_start_local_subflow_request", _record_direct)
    monkeypatch.setattr(engine, "ahandle_request", _record_dispatched)
    return recorded


def assert_subflow_failures(
    runs: list[RecordedSubflowRuns], expected: list[tuple[str, str]], *, level: int = ERROR_LEVEL
) -> None:
    """Assert the subflows that failed, as ``(flow name, result details)``, and the whole MCP form of each failure.

    A worker traceback in the details is masked on both sides of the comparison. ``level`` is the log level
    the failure's details carry, which is lower for a run whose requester reports the failure itself.
    """
    failed = [run for run in runs if run.result.failed()]
    assert [
        (run.request.flow_name, mask_worker_traceback(str(run.result.result_details)).text) for run in failed
    ] == expected
    for run in failed:
        assert isinstance(run.result, StartLocalSubflowResultFailure)
        assert masked_client_form(client_result(run.request, run.result)) == {
            "altered_workflow_state": False,
            "exception": None,
            "result_details": {
                "result_details": [
                    {"level": level, "message": mask_worker_traceback(str(run.result.result_details)).text}
                ]
            },
        }


async def execute_node(engine: Engine, node_name: str, node_type: str, library_name: str) -> ExecutedNode:
    """Send the ``ExecuteNodeRequest`` the executor sends for ``node_name``, and return it with its failure."""
    request = sent_from_open_workflow(
        ExecuteNodeRequest(node_name=node_name, node_metadata={"node_type": node_type, "library": library_name}),
        engine,
    )
    result = await engine.current_workflow().ahandle_request(request)
    assert isinstance(result, ExecuteNodeResultFailure), result
    return ExecutedNode(request=request, result=result)


def create_node_in_flow(  # noqa: PLR0913
    engine: Engine,
    node_type: str,
    node_name: str,
    flow_name: str,
    *,
    library_name: str,
    parent_group_name: str | None = None,
    metadata: dict[Any, Any] | None = None,
) -> str:
    """Create a node in ``flow_name``, inside the group ``parent_group_name`` when one is given."""
    with engine.context_manager.flow(flow_name):
        result = engine.handle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=library_name,
                node_name=node_name,
                parent_group_name=parent_group_name,
                metadata=metadata,
            )
        )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def new_flow(engine: Engine, workflow_name: str) -> str:
    """Open a workflow and create one top-level flow in it."""
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name
