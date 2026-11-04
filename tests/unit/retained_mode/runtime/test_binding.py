"""Which open workflow the current work acts on: the bound one, else the only open one."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender, ResultPayloadSuccess
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotOpenResultFailure
from griptape_nodes.retained_mode.managers.event_manager import ResultContext, current_request_type
from griptape_nodes.retained_mode.request_handlers import RequiredSender
from griptape_nodes.retained_mode.runtime.binding import (
    NO_WORKFLOW,
    WorkflowNotNamedError,
    WorkflowNotOpenError,
    bound_workflow,
    create_task_bound_to_no_workflow,
)
from griptape_nodes.retained_mode.runtime.open_workflow_set import NoOpenWorkflowError, OpenWorkflowSet
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


UNKNOWN_WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
_OTHER_ENGINE = RequestSender(engine_id="another-engine")


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request used only to see which workflow its handler acts on."""


@dataclass(kw_only=True)
class _ProbeResult(ResultPayloadSuccess):
    workflow: WorkflowRuntime | None = None


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    engine.context_manager.push_workflow(workflow_name=display_name)
    return engine.open_workflows.get(engine.context_manager.get_current_workflow_id())


def _count_handler_calls(engine: Engine) -> list[_ProbeRequest]:
    calls: list[_ProbeRequest] = []

    def handler(request: _ProbeRequest) -> _ProbeResult:
        calls.append(request)
        return _ProbeResult(result_details="ok")

    engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)
    return calls


def _record_current_workflow(engine: Engine) -> None:
    def handler(_request: _ProbeRequest) -> _ProbeResult:
        return _ProbeResult(result_details="ok", workflow=engine.current_workflow_or_none())

    engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)


class TestCurrentWorkflow:
    def test_unbound_work_acts_on_the_only_open_workflow(self, engine: Engine) -> None:
        workflow = _open(engine, "only")

        assert engine.current_workflow() is workflow
        assert engine.current_workflow_or_none() is workflow

    def test_with_none_open_reads_find_none_and_writes_raise(self, engine: Engine) -> None:
        assert engine.current_workflow_or_none() is None
        with pytest.raises(NoOpenWorkflowError, match="no workflow is open"):
            engine.current_workflow()

    def test_the_bound_workflow_comes_before_the_open_one(self, engine: Engine) -> None:
        _open(engine, "open")
        bound = WorkflowRuntime("bound")

        with engine.bind_workflow(bound):
            assert engine.current_workflow() is bound

        assert engine.current_workflow() is not bound

    def test_work_bound_to_no_workflow_has_none_even_with_one_open(self, engine: Engine) -> None:
        _open(engine, "open")

        with engine.bind_workflow(NO_WORKFLOW):
            assert engine.current_workflow_or_none() is None
            with pytest.raises(NoOpenWorkflowError):
                engine.current_workflow()

    def test_unbound_work_with_several_open_raises(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        # Several open cannot happen through `open`; force it to check the read refuses to guess.
        several = [WorkflowRuntime("abc"), WorkflowRuntime("def")]
        monkeypatch.setattr(engine.open_workflows, "all_open", lambda: several)

        with pytest.raises(WorkflowNotNamedError, match="2 workflows are open"):
            engine.current_workflow_or_none()
        with pytest.raises(WorkflowNotNamedError):
            engine.current_workflow()
        with engine.bind_workflow(several[1]):
            assert engine.current_workflow() is several[1]


class TestClosedWorkflowBinding:
    def test_work_bound_to_a_closed_workflow_is_refused(self, engine: Engine) -> None:
        workflow = _open(engine, "closing")

        with engine.bind_workflow(workflow):
            # Closed by other work, as a task started earlier sees it: the closing work itself
            # stops acting on it.
            contextvars.copy_context().run(engine.context_manager.pop_workflow)
            with pytest.raises(WorkflowNotOpenError, match="was closed"):
                engine.current_workflow_or_none()
            with pytest.raises(WorkflowNotOpenError, match="was closed"):
                engine.current_workflow()

    def test_the_work_that_closes_its_workflow_acts_on_no_workflow_after(self, engine: Engine) -> None:
        workflow = _open(engine, "closing")

        with engine.bind_workflow(workflow):
            engine.context_manager.pop_workflow()
            assert bound_workflow.get() is NO_WORKFLOW

    def test_a_request_that_closes_its_workflow_and_opens_another_acts_on_the_one_it_opened(
        self, engine: Engine
    ) -> None:
        """A clean-slate load closes the open workflow, then opens and builds into the next."""
        _open(engine, "closing")

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            engine.context_manager.pop_workflow()
            engine.context_manager.push_workflow(workflow_name="opened")
            return _ProbeResult(result_details="ok", workflow=engine.current_workflow())

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        result = engine.event_manager.handle_request(_ProbeRequest()).result

        assert isinstance(result, _ProbeResult)
        assert result.workflow is engine.open_workflows.all_open()[0]
        assert bound_workflow.get() is None

    def test_a_nested_request_whose_caller_is_bound_to_a_closed_workflow_is_refused(self, engine: Engine) -> None:
        workflow = _open(engine, "closing")
        calls = _count_handler_calls(engine)

        with engine.bind_workflow(workflow):
            contextvars.copy_context().run(engine.context_manager.pop_workflow)
            result_event = engine.event_manager.handle_request(_ProbeRequest())

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert calls == []


class TestTasksBoundToNoWorkflow:
    @pytest.mark.asyncio
    async def test_the_task_acts_on_no_workflow_and_the_caller_keeps_its_binding(self, engine: Engine) -> None:
        workflow = _open(engine, "open")

        async def read_binding() -> object:
            return bound_workflow.get()

        with engine.bind_workflow(workflow):
            task_binding = await create_task_bound_to_no_workflow(asyncio.get_running_loop(), read_binding())
            assert bound_workflow.get() is workflow

        assert task_binding is NO_WORKFLOW


class TestDispatchBindsTheEnvelopeWorkflow:
    def test_the_handler_acts_on_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        _record_current_workflow(engine)

        with engine.bind_workflow(NO_WORKFLOW):
            result_event = engine.event_manager.handle_request(
                _ProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is workflow

    @pytest.mark.asyncio
    async def test_the_async_handler_acts_on_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        _record_current_workflow(engine)

        with engine.bind_workflow(NO_WORKFLOW):
            result_event = await engine.event_manager.ahandle_request(
                _ProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is workflow

    def test_the_binding_ends_with_the_request(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        _record_current_workflow(engine)

        with engine.bind_workflow(NO_WORKFLOW):
            engine.event_manager.handle_request(
                _ProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
            assert engine.current_workflow_or_none() is None

    def test_a_request_with_no_envelope_id_acts_on_the_only_open_workflow(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        _record_current_workflow(engine)

        result_event = engine.event_manager.handle_request(_ProbeRequest())

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is workflow

    def test_a_nested_request_with_no_envelope_id_keeps_the_callers_binding(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        _record_current_workflow(engine)
        bound = WorkflowRuntime("bound")

        with engine.bind_workflow(bound):
            result_event = engine.event_manager.handle_request(_ProbeRequest())

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is bound
        assert result_event.result.workflow is not workflow

    def test_a_request_from_another_engine_with_no_envelope_id_acts_on_no_workflow(self, engine: Engine) -> None:
        _open(engine, "open")

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok", workflow=engine.current_workflow_or_none())

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler, sender=RequiredSender.ENGINE)

        result_event = engine.event_manager.handle_request(
            _ProbeRequest(), result_context=ResultContext(sender=_OTHER_ENGINE)
        )

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is None

    def test_a_request_from_another_engine_acts_on_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        workflow = _open(engine, "open")

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok", workflow=engine.current_workflow_or_none())

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler, sender=RequiredSender.ENGINE)

        result_event = engine.event_manager.handle_request(
            _ProbeRequest(), result_context=ResultContext(sender=_OTHER_ENGINE, open_workflow_id=workflow.workflow_id)
        )

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is workflow

    @pytest.mark.asyncio
    async def test_tasks_the_handler_spawns_keep_its_workflow_after_it_returns(self, engine: Engine) -> None:
        workflow = _open(engine, "open")
        release = asyncio.Event()
        spawned: list[asyncio.Task[WorkflowRuntime | None]] = []

        async def read_after_release() -> WorkflowRuntime | None:
            await release.wait()
            return engine.current_workflow_or_none()

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            spawned.append(asyncio.create_task(read_after_release()))
            return _ProbeResult(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        with engine.bind_workflow(NO_WORKFLOW):
            await engine.event_manager.ahandle_request(
                _ProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
            release.set()
            assert await spawned[0] is workflow

    @pytest.mark.asyncio
    async def test_tasks_a_request_with_no_envelope_id_spawns_never_reach_the_next_workflow(
        self, engine: Engine
    ) -> None:
        _open(engine, "first")
        release = asyncio.Event()
        spawned: list[asyncio.Task[WorkflowRuntime | None]] = []

        async def read_after_release() -> WorkflowRuntime | None:
            await release.wait()
            return engine.current_workflow_or_none()

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            spawned.append(asyncio.create_task(read_after_release()))
            return _ProbeResult(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        await engine.event_manager.ahandle_request(_ProbeRequest())
        engine.context_manager.pop_workflow()
        second = _open(engine, "second")
        release.set()

        with pytest.raises(WorkflowNotOpenError):
            await asyncio.wait_for(spawned[0], timeout=5)
        assert engine.current_workflow_or_none() is second

    def test_a_request_that_opens_the_first_workflow_acts_on_it(self, engine: Engine) -> None:
        opened: list[WorkflowRuntime] = []

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            opened.append(_open(engine, "opened"))
            return _ProbeResult(result_details="ok", workflow=engine.current_workflow_or_none())

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        result_event = engine.event_manager.handle_request(_ProbeRequest())

        assert isinstance(result_event.result, _ProbeResult)
        assert result_event.result.workflow is opened[0]

    def test_a_binding_the_handler_makes_with_none_open_ends_with_the_request(self, engine: Engine) -> None:
        bound = WorkflowRuntime("bound")

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            bound_workflow.set(bound)
            return _ProbeResult(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        engine.event_manager.handle_request(_ProbeRequest())

        assert bound_workflow.get() is None

    def test_an_envelope_id_naming_another_workflow_fails_without_running(self, engine: Engine) -> None:
        _open(engine, "open")
        calls = _count_handler_calls(engine)

        result_event = engine.event_manager.handle_request(
            _ProbeRequest(), result_context=ResultContext(open_workflow_id=UNKNOWN_WORKFLOW_ID)
        )

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert str(result_event.result.result_details) == (
            f"Attempted to handle _ProbeRequest in workflow '{UNKNOWN_WORKFLOW_ID}'. Failed because that "
            "workflow isn't open in the engine anymore. Reopen the workflow and try again."
        )
        assert result_event.open_workflow_id == UNKNOWN_WORKFLOW_ID
        assert calls == []

    def test_an_envelope_id_of_a_closed_workflow_fails_without_running(self, engine: Engine) -> None:
        closed_id = _open(engine, "closed").workflow_id
        engine.context_manager.pop_workflow()
        calls = _count_handler_calls(engine)

        result_event = engine.event_manager.handle_request(
            _ProbeRequest(), result_context=ResultContext(open_workflow_id=closed_id)
        )

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert calls == []

    def test_a_workflow_closing_between_the_check_and_the_lookup_fails_the_request(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A check that still sees the workflow open, then a lookup that finds it gone, as when a close
        # on another thread lands between them.
        closed_id = _open(engine, "closed").workflow_id
        engine.context_manager.pop_workflow()
        monkeypatch.setattr(OpenWorkflowSet, "is_open", lambda _self, _workflow_id: True)
        calls = _count_handler_calls(engine)

        result_event = engine.event_manager.handle_request(
            _ProbeRequest(), result_context=ResultContext(open_workflow_id=closed_id)
        )

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert calls == []
        assert current_request_type() is None
        assert bound_workflow.get() is None

    @pytest.mark.asyncio
    async def test_the_async_path_fails_an_envelope_id_naming_no_open_workflow(self, engine: Engine) -> None:
        _record_current_workflow(engine)

        result_event = await engine.event_manager.ahandle_request(
            _ProbeRequest(), result_context=ResultContext(open_workflow_id=UNKNOWN_WORKFLOW_ID)
        )

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
