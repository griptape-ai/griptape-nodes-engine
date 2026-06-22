import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.servers import mcp as mcp_module
from griptape_nodes.servers.mcp import (
    _BATCH_MAX_AUTO_TIMEOUT_MS,
    _BATCH_PER_REQUEST_TIMEOUT_MS,
    EVENT_REQUEST_BATCH_TOOL_NAME,
    SUPPORTED_REQUEST_EVENTS,
    _build_batch_pairs,
    _dispatch_to_engine,
    _event_request_batch_input_schema,
    _resolve_batch_timeout_ms,
    _summarize_result_details,
    _trim_batch_results,
    _trim_response,
)


class TestSummarizeResultDetails:
    def test_returns_none_for_none(self) -> None:
        assert _summarize_result_details(None) is None

    def test_passes_strings_through(self) -> None:
        assert _summarize_result_details("already a string") == "already a string"

    def test_joins_messages_from_nested_result_details(self) -> None:
        payload = {
            "result_details": [
                {"level": 20, "message": "first"},
                {"level": 10, "message": "second"},
            ]
        }

        assert _summarize_result_details(payload) == "first\nsecond"

    def test_returns_inner_list_when_all_messages_empty(self) -> None:
        payload = {"result_details": [{"level": 20, "message": ""}]}

        # Fall back to the raw list so we never hide data we did not recognize.
        assert _summarize_result_details(payload) == [{"level": 20, "message": ""}]

    def test_returns_input_unchanged_for_unrecognized_shape(self) -> None:
        payload = {"something_else": 1}

        assert _summarize_result_details(payload) == payload


class TestTrimResponse:
    def test_drops_envelope_noise_and_flattens_details(self) -> None:
        raw = {
            "engine_id": "engine-1",
            "session_id": "session-1",
            "request": {"node_type": "Probe", "library": "demo"},
            "request_id": "abc",
            "response_topic": "response",
            "retained_mode": "cmd.create_node(...)",
            "event_type": "EventResultSuccess",
            "request_type": "CreateNodeRequest",
            "result_type": "CreateNodeResultSuccess",
            "result": {
                "result_details": {
                    "result_details": [
                        {"level": 10, "message": "Created node 'Probe_1'"},
                    ]
                },
                "altered_workflow_state": True,
                "node_name": "Probe_1",
            },
        }

        trimmed = _trim_response(raw)

        assert trimmed == {
            "ok": True,
            "details": "Created node 'Probe_1'",
            "altered_workflow_state": True,
            "node_name": "Probe_1",
        }

    def test_marks_failures_with_ok_false(self) -> None:
        raw = {
            "result_type": "CreateNodeResultFailure",
            "result": {
                "result_details": {
                    "result_details": [{"level": 40, "message": "boom"}],
                },
                "altered_workflow_state": False,
            },
        }

        trimmed = _trim_response(raw)

        assert trimmed["ok"] is False
        assert trimmed["details"] == "boom"
        assert trimmed["altered_workflow_state"] is False

    def test_handles_missing_result_gracefully(self) -> None:
        trimmed = _trim_response({"result_type": "SomethingSuccess"})

        assert trimmed == {"ok": True}


class TestEventRequestBatchInputSchema:
    def test_enumerates_every_supported_request_type(self) -> None:
        schema = _event_request_batch_input_schema()

        request_type_enum = schema["properties"]["requests"]["items"]["properties"]["request_type"]["enum"]
        assert set(request_type_enum) == set(SUPPORTED_REQUEST_EVENTS)

    def test_marks_requests_required_and_non_empty(self) -> None:
        schema = _event_request_batch_input_schema()

        assert schema["required"] == ["requests"]
        assert schema["properties"]["requests"]["minItems"] == 1


class TestBuildBatchPairs:
    def test_builds_pairs_for_valid_inner_requests(self) -> None:
        raw = [
            {"request_type": "CreateNodeRequest", "request": {"node_type": "TextInput"}},
            {
                "request_type": "CreateConnectionRequest",
                "request": {
                    "source_parameter_name": "text",
                    "target_parameter_name": "prompt",
                    "source_node_name": "TextInput_1",
                    "target_node_name": "Agent_1",
                },
            },
        ]

        pairs = _build_batch_pairs(raw)

        assert [request_type for request_type, _ in pairs] == ["CreateNodeRequest", "CreateConnectionRequest"]
        # Defaults from the dataclasses are filled in, so payload dicts carry every field.
        assert pairs[0][1]["node_type"] == "TextInput"
        assert pairs[1][1]["source_node_name"] == "TextInput_1"

    def test_rejects_non_list_requests(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            _build_batch_pairs("not a list")

    def test_rejects_empty_list(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            _build_batch_pairs([])

    def test_rejects_non_dict_entry(self) -> None:
        with pytest.raises(TypeError, match="entry 0"):
            _build_batch_pairs(["not a dict"])

    def test_rejects_unknown_request_type(self) -> None:
        with pytest.raises(ValueError, match="is not a supported tool"):
            _build_batch_pairs([{"request_type": "NopeRequest", "request": {}}])

    def test_rejects_non_dict_request_payload(self) -> None:
        with pytest.raises(TypeError, match="must be an object"):
            _build_batch_pairs([{"request_type": "ListRegisteredLibrariesRequest", "request": "bad"}])

    def test_rejects_unknown_kwargs_in_inner_payload(self) -> None:
        with pytest.raises(ValueError, match="Attempted to construct CreateNodeRequest"):
            _build_batch_pairs([{"request_type": "CreateNodeRequest", "request": {"node_type": "X", "bogus_field": 1}}])


class TestResolveBatchTimeoutMs:
    _BATCH_OF_FOUR = 4
    _LARGE_BATCH = 100
    _EXPLICIT_OVERRIDE_MS = 15000

    def test_scales_default_with_batch_size(self) -> None:
        assert (
            _resolve_batch_timeout_ms(None, self._BATCH_OF_FOUR) == _BATCH_PER_REQUEST_TIMEOUT_MS * self._BATCH_OF_FOUR
        )

    def test_caps_default_at_ceiling(self) -> None:
        # A 100-call batch would scale past the cap; the helper clamps it.
        assert _resolve_batch_timeout_ms(None, self._LARGE_BATCH) == _BATCH_MAX_AUTO_TIMEOUT_MS

    def test_passes_through_explicit_override(self) -> None:
        assert _resolve_batch_timeout_ms(self._EXPLICIT_OVERRIDE_MS, self._BATCH_OF_FOUR) == self._EXPLICIT_OVERRIDE_MS

    def test_rejects_non_int_override(self) -> None:
        with pytest.raises(TypeError, match="must be a positive integer"):
            _resolve_batch_timeout_ms("15s", self._BATCH_OF_FOUR)

    def test_rejects_zero_or_negative_override(self) -> None:
        with pytest.raises(ValueError, match="must be a positive integer"):
            _resolve_batch_timeout_ms(0, self._BATCH_OF_FOUR)
        with pytest.raises(ValueError, match="must be a positive integer"):
            _resolve_batch_timeout_ms(-1, self._BATCH_OF_FOUR)

    def test_rejects_bool_override(self) -> None:
        # bools are technically ints in Python; reject explicitly so True does not become 1ms.
        with pytest.raises(TypeError, match="must be a positive integer"):
            _resolve_batch_timeout_ms(True, self._BATCH_OF_FOUR)


class TestTrimBatchResults:
    def test_trims_each_inner_response(self) -> None:
        raw = [
            {
                "result_type": "CreateNodeResultSuccess",
                "result": {
                    "result_details": {"result_details": [{"level": 10, "message": "a"}]},
                    "node_name": "A_1",
                },
            },
            {
                "result_type": "CreateNodeResultSuccess",
                "result": {
                    "result_details": {"result_details": [{"level": 10, "message": "b"}]},
                    "node_name": "B_1",
                },
            },
        ]

        trimmed = _trim_batch_results(raw)

        assert trimmed == [
            {"ok": True, "details": "a", "node_name": "A_1"},
            {"ok": True, "details": "b", "node_name": "B_1"},
        ]

    def test_maps_exception_slots_to_failure_responses(self) -> None:
        raw = [
            Exception("boom"),
            {
                "result_type": "CreateNodeResultSuccess",
                "result": {
                    "result_details": {"result_details": [{"level": 10, "message": "ok"}]},
                    "node_name": "A_1",
                },
            },
        ]

        trimmed = _trim_batch_results(raw)

        assert trimmed[0] == {"ok": False, "details": "boom"}
        assert trimmed[1]["ok"] is True
        assert trimmed[1]["node_name"] == "A_1"


class TestEventRequestBatchToolName:
    def test_is_not_a_supported_request_event(self) -> None:
        # The batch tool is intentionally synthetic; gating it on SUPPORTED_REQUEST_EVENTS would
        # require a fake RequestPayload subclass and break call_tool's payload-class lookup.
        assert EVENT_REQUEST_BATCH_TOOL_NAME not in SUPPORTED_REQUEST_EVENTS


class TestDispatchToEngineShield:
    """A client-side timeout must not cancel the in-flight engine operation.

    Regression coverage for griptape-nodes-engine#4883: when a multi-node resolve runs
    longer than the MCP dispatch timeout, cancelling the engine coroutine strands the node
    it is executing in RESOLVING forever. _dispatch_to_engine shields the engine-side future
    so the timeout raises here while the engine runs to completion on its own loop.
    """

    @staticmethod
    def _a_request_payload() -> RequestPayload:
        # _handle_request_on_engine_loop is patched in these tests, so the payload is never
        # dispatched; any real RequestPayload satisfies the signature.
        return SUPPORTED_REQUEST_EVENTS["ListRegisteredLibrariesRequest"]()

    @staticmethod
    def _run_engine_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
        engine_loop = asyncio.new_event_loop()
        thread = threading.Thread(target=engine_loop.run_forever, daemon=True, name="test-engine-loop")
        thread.start()
        return engine_loop, thread

    @staticmethod
    def _stop_engine_loop(engine_loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
        engine_loop.call_soon_threadsafe(engine_loop.stop)
        thread.join(timeout=2)
        engine_loop.close()

    @pytest.mark.asyncio
    async def test_timeout_does_not_cancel_engine_work(self) -> None:
        engine_loop, thread = self._run_engine_loop_in_thread()
        completed = threading.Event()
        cancelled = threading.Event()

        async def slow_engine_handler(_payload: object) -> dict[str, bool]:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            completed.set()
            return {"ok": True}

        event_manager = MagicMock()
        event_manager.event_loop = engine_loop
        try:
            with (
                patch.object(mcp_module.GriptapeNodes, "EventManager", return_value=event_manager),
                patch.object(mcp_module, "_handle_request_on_engine_loop", slow_engine_handler),
            ):
                with pytest.raises(TimeoutError):
                    await _dispatch_to_engine(self._a_request_payload(), timeout_ms=50)

                # The shielded engine coroutine keeps running on its own loop after the
                # wait times out; wait long enough for its 0.5s body to finish.
                await asyncio.sleep(0.75)

            assert completed.is_set(), "engine work should run to completion after a client timeout"
            assert not cancelled.is_set(), "engine work must not be cancelled by a client timeout"
        finally:
            self._stop_engine_loop(engine_loop, thread)

    @pytest.mark.asyncio
    async def test_returns_result_when_engine_finishes_before_timeout(self) -> None:
        engine_loop, thread = self._run_engine_loop_in_thread()

        async def fast_engine_handler(_payload: object) -> dict[str, bool]:
            return {"ok": True}

        event_manager = MagicMock()
        event_manager.event_loop = engine_loop
        try:
            with (
                patch.object(mcp_module.GriptapeNodes, "EventManager", return_value=event_manager),
                patch.object(mcp_module, "_handle_request_on_engine_loop", fast_engine_handler),
            ):
                result = await _dispatch_to_engine(self._a_request_payload(), timeout_ms=5000)

            assert result == {"ok": True}
        finally:
            self._stop_engine_loop(engine_loop, thread)
