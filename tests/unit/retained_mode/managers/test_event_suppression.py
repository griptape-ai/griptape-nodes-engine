"""Event suppression: what it matches, and how far it reaches.

``EventSuppressionContext`` hides engine-internal bookkeeping from editors -- the node copies and
short-lived flows a loop rebuilds its body into. Two things have to hold for that to be safe, and
neither was covered before: the suppression set has to actually *match* the events the engine
emits, and it must not reach a request the engine did not dispatch from inside the window.

The matching half went untested for long enough to stop working entirely. Suppression sets are
written in terms of result payload classes, but request results arrive as an
``EventResultSuccess`` that carries its payload under ``result`` -- an attribute the matcher never
looked at -- so every entry silently matched nothing. The existing tests in
``test_griptape_nodes.py`` patch ``should_suppress_event`` wholesale, which is why nothing noticed.

The reach half matters because the suppressed types (``SetParameterValueResultSuccess``,
``CreateConnectionResultSuccess``, ``CreateNodeResultSuccess``) are ones an editor also requests
on its own behalf. Suppression keyed only by type and stored on the manager would drop a client's
own confirmations whenever a loop happened to be mid-rebuild, leaving the editor displaying state
the engine does not have.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from griptape_nodes.retained_mode.events.base_events import (
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    GriptapeNodeEvent,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CurrentControlNodeEvent,
    NodeResolvedEvent,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultFailure,
    CreateNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.retained_mode.managers.event_manager import EventManager, EventSuppressionContext


def _create_node_result_event() -> EventResultSuccess:
    """A request result shaped exactly as the engine hands it to should_suppress_event.

    ``engine.handle_request`` passes the ``EventResultSuccess`` itself, not the
    ``GriptapeNodeEvent`` it later wraps it in, so this is the shape that has to match.
    """
    return EventResultSuccess(
        request=CreateNodeRequest(node_type="Note", node_name="Body Node"),
        result=CreateNodeResultSuccess(
            node_name="Body Node",
            node_type="Note",
            parent_flow_name="ControlFlow_2",
            result_details="Created node 'Body Node'.",
        ),
    )


def _create_node_failure_event() -> EventResultFailure:
    return EventResultFailure(
        request=CreateNodeRequest(node_type="Note", node_name="Body Node"),
        result=CreateNodeResultFailure(result_details="Could not create node 'Body Node'."),
    )


class TestShouldSuppressEventMatching:
    """Which shapes a suppression set matches."""

    def test_nothing_is_suppressed_by_default(self) -> None:
        event_manager = EventManager()
        assert event_manager.should_suppress_event(_create_node_result_event()) is False

    def test_matches_the_result_payload_of_a_request_result(self) -> None:
        """The shape the engine actually checks. This is the branch that was missing."""
        event_manager = EventManager()
        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            assert event_manager.should_suppress_event(_create_node_result_event()) is True

    def test_does_not_match_an_unlisted_result_payload(self) -> None:
        """Suppressing one result type must not suppress its neighbours."""
        event_manager = EventManager()
        with EventSuppressionContext(event_manager, {SetParameterValueResultSuccess}):
            assert event_manager.should_suppress_event(_create_node_result_event()) is False

    def test_matches_a_failure_result_payload_independently(self) -> None:
        event_manager = EventManager()
        with EventSuppressionContext(event_manager, {CreateNodeResultFailure}):
            assert event_manager.should_suppress_event(_create_node_failure_event()) is True
            assert event_manager.should_suppress_event(_create_node_result_event()) is False

    def test_matches_a_result_payload_already_wrapped_for_broadcast(self) -> None:
        """Callers that pass the outer GriptapeNodeEvent must get the same answer."""
        event_manager = EventManager()
        wrapped = GriptapeNodeEvent(wrapped_event=_create_node_result_event())
        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            assert event_manager.should_suppress_event(wrapped) is True

    def test_matches_an_execution_payload_under_wrapped_event(self) -> None:
        """The pre-existing branch: ExecutionEvent names its payload `payload`, not `result`."""
        event_manager = EventManager()
        event = ExecutionGriptapeNodeEvent(
            wrapped_event=ExecutionEvent(payload=CurrentControlNodeEvent(node_name="Blur"))
        )
        with EventSuppressionContext(event_manager, {CurrentControlNodeEvent}):
            assert event_manager.should_suppress_event(event) is True

    def test_does_not_match_an_unlisted_execution_payload(self) -> None:
        event_manager = EventManager()
        event = ExecutionGriptapeNodeEvent(
            wrapped_event=ExecutionEvent(payload=CurrentControlNodeEvent(node_name="Blur"))
        )
        with EventSuppressionContext(event_manager, {NodeResolvedEvent}):
            assert event_manager.should_suppress_event(event) is False

    def test_matches_the_wrapper_type_itself(self) -> None:
        """A set may name the wrapper rather than the payload."""
        event_manager = EventManager()
        wrapped = GriptapeNodeEvent(wrapped_event=_create_node_result_event())
        with EventSuppressionContext(event_manager, {GriptapeNodeEvent}):
            assert event_manager.should_suppress_event(wrapped) is True


class TestSuppressionWindowLifetime:
    """A window must close exactly when its block does, however the block ends."""

    def test_suppression_ends_with_the_block(self) -> None:
        event_manager = EventManager()
        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            pass
        assert event_manager.should_suppress_event(_create_node_result_event()) is False

    def test_suppression_ends_when_the_block_raises(self) -> None:
        """Loop teardown runs in `finally` blocks; a failed iteration must not leave events dark."""
        event_manager = EventManager()

        def fail_inside_the_window() -> None:
            with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
                msg = "iteration failed"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="iteration failed"):
            fail_inside_the_window()

        assert event_manager.should_suppress_event(_create_node_result_event()) is False

    def test_nested_windows_compose(self) -> None:
        """An inner window adds to the outer one and restores it, rather than replacing it."""
        event_manager = EventManager()
        parameter_event = EventResultSuccess(
            request=CreateNodeRequest(node_type="Note"),
            result=SetParameterValueResultSuccess(
                finalized_value=1,
                data_type="int",
                result_details="Set parameter value.",
            ),
        )

        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            with EventSuppressionContext(event_manager, {SetParameterValueResultSuccess}):
                assert event_manager.should_suppress_event(_create_node_result_event()) is True
                assert event_manager.should_suppress_event(parameter_event) is True

            # Leaving the inner window releases only what the inner window added.
            assert event_manager.should_suppress_event(_create_node_result_event()) is True
            assert event_manager.should_suppress_event(parameter_event) is False

    def test_clear_event_suppression_releases_everything(self) -> None:
        event_manager = EventManager()
        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            event_manager.clear_event_suppression()
            assert event_manager.should_suppress_event(_create_node_result_event()) is False


class TestSuppressionIsScopedToItsCaller:
    """The regression this design exists to prevent: suppression reaching a client's own request.

    Loop windows span awaits (``_delete_iteration_flows``) and whole parallel runs, so "nothing
    else can be in flight" is not a property the engine has. These tests pin that a window is
    invisible outside the context that opened it.
    """

    def test_a_concurrent_task_is_unaffected(self) -> None:
        """An editor's request handled while a loop rebuilds its body still gets broadcast."""
        event_manager = EventManager()
        window_open = asyncio.Event()
        observed_by_editor: list[bool] = []

        async def loop_rebuilding_its_body() -> None:
            with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
                window_open.set()
                # An await inside the window: this is where another task gets to run.
                await asyncio.sleep(0)
                assert event_manager.should_suppress_event(_create_node_result_event()) is True

        async def editor_creating_a_node() -> None:
            await window_open.wait()
            observed_by_editor.append(event_manager.should_suppress_event(_create_node_result_event()))

        async def run_both() -> None:
            await asyncio.gather(loop_rebuilding_its_body(), editor_creating_a_node())

        asyncio.run(run_both())

        assert observed_by_editor == [False], (
            "A suppression window opened by the engine reached a concurrently-handled request. "
            "The editor would never be told about a node it just created."
        )

    def test_another_thread_is_unaffected(self) -> None:
        """handle_request runs on arbitrary threads, so thread reach matters as much as task reach."""
        event_manager = EventManager()
        window_open = threading.Event()
        observed_on_other_thread: list[bool] = []

        def observe() -> None:
            window_open.wait(timeout=5)
            observed_on_other_thread.append(event_manager.should_suppress_event(_create_node_result_event()))

        observer = threading.Thread(target=observe)
        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            observer.start()
            window_open.set()
            observer.join(timeout=5)

        assert observed_on_other_thread == [False]

    def test_a_nested_request_inherits_suppression(self) -> None:
        """The other half of the contract: the lineage meant to be hidden stays hidden.

        Suppression has to survive into the requests a suppressed handler dispatches -- the
        transient node creations come from inside ``on_deserialize_node_from_commands``, not from
        the call the executor makes directly.
        """
        event_manager = EventManager()

        def nested_handler() -> bool:
            return event_manager.should_suppress_event(_create_node_result_event())

        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            assert nested_handler() is True

    @pytest.mark.asyncio
    async def test_a_nested_await_inherits_suppression(self) -> None:
        """Same, for the async dispatch path."""
        event_manager = EventManager()

        async def nested_handler() -> bool:
            await asyncio.sleep(0)
            return event_manager.should_suppress_event(_create_node_result_event())

        with EventSuppressionContext(event_manager, {CreateNodeResultSuccess}):
            assert await nested_handler() is True
