import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams, ToolAnnotations
from starlette.requests import Request

from griptape_nodes.node_library.advanced_node_library import MCPTool
from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.servers import mcp as mcp_module
from griptape_nodes.servers.mcp import (
    _BATCH_MAX_AUTO_TIMEOUT_MS,
    _BATCH_PER_REQUEST_TIMEOUT_MS,
    ENGINE_TOOLS,
    EVENT_REQUEST_BATCH_TOOL_NAME,
    SUPPORTED_REQUEST_EVENTS,
    _build_batch_requests,
    _dispatch_to_engine,
    _event_request_batch_input_schema,
    _resolve_batch_timeout_ms,
    _summarize_result_details,
    _tool_selection_for_request,
    _tools_for_request,
    _trim_batch_results,
    _trim_response,
    call_tool,
    list_tools,
)

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext

# A context with no HTTP request, which the handlers serve the engine tools to.
_NO_CONTEXT = cast("ServerRequestContext[Any]", SimpleNamespace(request=None))


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
        schema = _event_request_batch_input_schema(["CreateNodeRequest", "ListRegisteredLibrariesRequest"])

        request_type_enum = schema["properties"]["requests"]["items"]["properties"]["request_type"]["enum"]
        assert request_type_enum == ["CreateNodeRequest", "ListRegisteredLibrariesRequest"]

    def test_marks_requests_required_and_non_empty(self) -> None:
        schema = _event_request_batch_input_schema(sorted(ENGINE_TOOLS))

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

        requests = _build_batch_requests(raw, ENGINE_TOOLS)

        assert [type(request).__name__ for request in requests] == ["CreateNodeRequest", "CreateConnectionRequest"]
        assert requests[0].node_type == "TextInput"  # type: ignore[attr-defined]
        assert requests[1].source_node_name == "TextInput_1"  # type: ignore[attr-defined]

    def test_rejects_non_list_requests(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            _build_batch_requests("not a list", ENGINE_TOOLS)

    def test_rejects_empty_list(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            _build_batch_requests([], ENGINE_TOOLS)

    def test_rejects_non_dict_entry(self) -> None:
        with pytest.raises(TypeError, match="entry 0"):
            _build_batch_requests(["not a dict"], ENGINE_TOOLS)

    def test_rejects_unknown_request_type(self) -> None:
        with pytest.raises(ValueError, match="is not a supported tool"):
            _build_batch_requests([{"request_type": "NopeRequest", "request": {}}], ENGINE_TOOLS)

    def test_rejects_non_dict_request_payload(self) -> None:
        with pytest.raises(TypeError, match="must be an object"):
            _build_batch_requests([{"request_type": "ListRegisteredLibrariesRequest", "request": "bad"}], ENGINE_TOOLS)

    def test_rejects_unknown_kwargs_in_inner_payload(self) -> None:
        with pytest.raises(ValueError, match="Attempted to construct CreateNodeRequest"):
            _build_batch_requests(
                [{"request_type": "CreateNodeRequest", "request": {"node_type": "X", "bogus_field": 1}}], ENGINE_TOOLS
            )


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


class TestListTools:
    @pytest.mark.asyncio
    async def test_advertises_every_request_event_plus_the_batch_envelope(self) -> None:
        result = await list_tools(_NO_CONTEXT, None)

        names = {tool.name for tool in result.tools}
        assert names == set(SUPPORTED_REQUEST_EVENTS) | {EVENT_REQUEST_BATCH_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_advertises_flow_recovery_requests(self) -> None:
        result = await list_tools(_NO_CONTEXT, None)

        names = {tool.name for tool in result.tools}
        assert {"CancelFlowRequest", "UnresolveFlowRequest"} <= names

    @pytest.mark.asyncio
    async def test_advertises_flow_validation_request(self) -> None:
        result = await list_tools(_NO_CONTEXT, None)

        names = {tool.name for tool in result.tools}
        assert "ValidateFlowDependenciesRequest" in names

    @pytest.mark.asyncio
    async def test_list_node_types_tool_offers_optional_include_metadata(self) -> None:
        result = await list_tools(_NO_CONTEXT, None)

        tools_by_name = {tool.name: tool for tool in result.tools}
        list_tool = tools_by_name["ListNodeTypesInLibraryRequest"]
        assert "include_metadata" in list_tool.input_schema["properties"]
        assert "include_metadata" not in list_tool.input_schema.get("required", [])

    @pytest.mark.asyncio
    async def test_every_tool_carries_an_object_input_schema(self) -> None:
        result = await list_tools(_NO_CONTEXT, None)

        assert all(tool.input_schema.get("type") == "object" for tool in result.tools)


class TestCallTool:
    """The 2.x low-level server no longer converts a handler raise into an error result.

    `call_tool` catches its own failures so a bad tool name or bad arguments still reach the
    client as `CallToolResult(is_error=True)` text it can read and retry from, rather than a
    JSON-RPC protocol error that carries no actionable message.
    """

    @pytest.mark.asyncio
    async def test_returns_trimmed_payload_as_text_content(self) -> None:
        async def fake_dispatch(_payload: object, timeout_ms: int | None = None) -> dict[str, Any]:  # noqa: ARG001
            return {"result_type": "ListRegisteredLibrariesResultSuccess", "result": {"libraries": ["demo"]}}

        with patch.object(mcp_module, "_dispatch_to_engine", fake_dispatch):
            result = await call_tool(
                _NO_CONTEXT, CallToolRequestParams(name="ListRegisteredLibrariesRequest", arguments={})
            )

        assert result.is_error is False
        assert json.loads(result.content[0].text) == {"ok": True, "libraries": ["demo"]}  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_treats_missing_arguments_as_empty(self) -> None:
        seen: dict[str, object] = {}

        async def fake_dispatch(payload: object, timeout_ms: int | None = None) -> dict[str, Any]:  # noqa: ARG001
            seen["payload"] = payload
            return {"result_type": "ListRegisteredLibrariesResultSuccess", "result": {}}

        with patch.object(mcp_module, "_dispatch_to_engine", fake_dispatch):
            result = await call_tool(_NO_CONTEXT, CallToolRequestParams(name="ListRegisteredLibrariesRequest"))

        assert result.is_error is False
        assert isinstance(seen["payload"], SUPPORTED_REQUEST_EVENTS["ListRegisteredLibrariesRequest"])

    @pytest.mark.asyncio
    async def test_reports_unsupported_tool_as_error_result(self) -> None:
        result = await call_tool(_NO_CONTEXT, CallToolRequestParams(name="NopeRequest", arguments={}))

        assert result.is_error is True
        assert "Unsupported tool: NopeRequest" in result.content[0].text  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_reports_bad_arguments_as_error_result(self) -> None:
        result = await call_tool(
            _NO_CONTEXT, CallToolRequestParams(name="CreateNodeRequest", arguments={"bogus_field": 1})
        )

        assert result.is_error is True
        assert result.content[0].text  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_reports_dispatch_timeout_as_error_result(self) -> None:
        async def timing_out_dispatch(_payload: object, timeout_ms: int | None = None) -> dict[str, Any]:  # noqa: ARG001
            raise TimeoutError

        with patch.object(mcp_module, "_dispatch_to_engine", timing_out_dispatch):
            result = await call_tool(
                _NO_CONTEXT, CallToolRequestParams(name="ListRegisteredLibrariesRequest", arguments={})
            )

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_dispatches_the_batch_envelope(self) -> None:
        async def fake_batch_dispatch(requests: list[RequestPayload], timeout_ms: int) -> list[Any]:  # noqa: ARG001
            return [{"result_type": "CreateNodeResultSuccess", "result": {"node_name": "A_1"}} for _ in requests]

        with patch.object(mcp_module, "_dispatch_batch_to_engine", fake_batch_dispatch):
            result = await call_tool(
                _NO_CONTEXT,
                CallToolRequestParams(
                    name=EVENT_REQUEST_BATCH_TOOL_NAME,
                    arguments={"requests": [{"request_type": "CreateNodeRequest", "request": {"node_type": "X"}}]},
                ),
            )

        assert result.is_error is False
        assert json.loads(result.content[0].text) == [{"ok": True, "node_name": "A_1"}]  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_reports_malformed_batch_as_error_result(self) -> None:
        result = await call_tool(
            _NO_CONTEXT, CallToolRequestParams(name=EVENT_REQUEST_BATCH_TOOL_NAME, arguments={"requests": []})
        )

        assert result.is_error is True
        assert "empty" in result.content[0].text  # type: ignore[union-attr]


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


@dataclass
class ProbeListRendersRequest(RequestPayload):
    """List the probe library's renders."""


@dataclass
class ProbeStartRenderRequest(RequestPayload):
    """Start a probe render."""

    frame: int = 1


_PROBE_LIBRARY_NAME = "Probe Render Library"
_PROBE_LIBRARY_SLUG = "probe_render_library"
_PROBE_TOOLS = [MCPTool(ProbeListRendersRequest, read_only=True), MCPTool(ProbeStartRenderRequest)]


def _context_for(url_path: str, query_string: bytes = b"") -> "ServerRequestContext[Any]":
    """A context whose HTTP request arrived at url_path on the server's /mcp mount."""
    request = Request(
        {"type": "http", "path": url_path, "root_path": "/mcp", "query_string": query_string, "headers": []}
    )
    return cast("ServerRequestContext[Any]", SimpleNamespace(request=request))


@pytest.fixture
def probe_library() -> Iterator[None]:
    """Load a library exposing _PROBE_TOOLS, read on the calling thread instead of the engine loop."""
    library = MagicMock()
    library.get_mcp_tools.return_value = list(_PROBE_TOOLS)

    async def run_inline(func: Callable[[], Any]) -> Any:
        return func()

    with (
        patch.object(mcp_module, "_run_on_engine_loop", run_inline),
        patch.object(mcp_module.LibraryRegistry, "list_libraries", return_value=[_PROBE_LIBRARY_NAME]),
        patch.object(mcp_module.LibraryRegistry, "get_library", return_value=library),
    ):
        yield


class TestToolSelectionForRequest:
    def test_engine_endpoint_serves_engine_tools_only(self) -> None:
        selection = _tool_selection_for_request(_context_for("/mcp/").request)

        assert selection.include_engine_tools is True
        assert selection.library_slugs == frozenset()

    def test_libraries_query_adds_libraries_to_engine_tools(self) -> None:
        selection = _tool_selection_for_request(_context_for("/mcp/", b"libraries=a,%20b,,").request)

        assert selection.include_engine_tools is True
        assert selection.library_slugs == frozenset({"a", "b"})

    @pytest.mark.parametrize("url_path", ["/mcp/libraries/nuke/", "/mcp/libraries/nuke"])
    def test_library_endpoint_serves_one_library_only(self, url_path: str) -> None:
        selection = _tool_selection_for_request(_context_for(url_path).request)

        assert selection.include_engine_tools is False
        assert selection.library_slugs == frozenset({"nuke"})

    def test_non_http_request_gets_engine_tools(self) -> None:
        selection = _tool_selection_for_request(None)

        assert selection.include_engine_tools is True
        assert selection.library_slugs == frozenset()


@pytest.mark.usefixtures("probe_library")
class TestLibraryTools:
    @pytest.mark.asyncio
    async def test_library_endpoint_lists_only_its_library_tools(self) -> None:
        result = await list_tools(_context_for(f"/mcp/libraries/{_PROBE_LIBRARY_SLUG}/"), None)

        names = {tool.name for tool in result.tools}
        assert names == {"ProbeListRendersRequest", "ProbeStartRenderRequest", EVENT_REQUEST_BATCH_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_engine_endpoint_hides_library_tools(self) -> None:
        result = await list_tools(_context_for("/mcp/"), None)

        names = {tool.name for tool in result.tools}
        assert "ProbeListRendersRequest" not in names

    @pytest.mark.asyncio
    async def test_libraries_query_adds_library_tools_to_engine_tools(self) -> None:
        result = await list_tools(_context_for("/mcp/", f"libraries={_PROBE_LIBRARY_SLUG}".encode()), None)

        names = {tool.name for tool in result.tools}
        assert names == set(ENGINE_TOOLS) | {
            "ProbeListRendersRequest",
            "ProbeStartRenderRequest",
            EVENT_REQUEST_BATCH_TOOL_NAME,
        }

    @pytest.mark.asyncio
    async def test_marks_only_read_only_tools_with_the_hint(self) -> None:
        result = await list_tools(_context_for(f"/mcp/libraries/{_PROBE_LIBRARY_SLUG}/"), None)

        tools_by_name = {tool.name: tool for tool in result.tools}
        assert tools_by_name["ProbeListRendersRequest"].annotations == ToolAnnotations(read_only_hint=True)
        assert tools_by_name["ProbeStartRenderRequest"].annotations is None

    @pytest.mark.asyncio
    async def test_batch_tool_enumerates_the_endpoints_tools(self) -> None:
        result = await list_tools(_context_for(f"/mcp/libraries/{_PROBE_LIBRARY_SLUG}/"), None)

        batch_tool = next(tool for tool in result.tools if tool.name == EVENT_REQUEST_BATCH_TOOL_NAME)
        request_type_schema = batch_tool.input_schema["properties"]["requests"]["items"]["properties"]["request_type"]
        assert request_type_schema["enum"] == ["ProbeListRendersRequest", "ProbeStartRenderRequest"]

    @pytest.mark.asyncio
    async def test_unknown_library_endpoint_fails_with_its_name(self) -> None:
        with pytest.raises(MCPError, match="library 'missing'"):
            await list_tools(_context_for("/mcp/libraries/missing/"), None)

    @pytest.mark.asyncio
    async def test_unknown_library_in_query_is_ignored(self) -> None:
        result = await list_tools(_context_for("/mcp/", b"libraries=missing"), None)

        names = {tool.name for tool in result.tools}
        assert names == set(ENGINE_TOOLS) | {EVENT_REQUEST_BATCH_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_calls_a_library_tool(self) -> None:
        seen: dict[str, object] = {}

        async def fake_dispatch(payload: object, timeout_ms: int | None = None) -> dict[str, Any]:  # noqa: ARG001
            seen["payload"] = payload
            return {"result_type": "ProbeStartRenderResultSuccess", "result": {}}

        with patch.object(mcp_module, "_dispatch_to_engine", fake_dispatch):
            result = await call_tool(
                _context_for(f"/mcp/libraries/{_PROBE_LIBRARY_SLUG}/"),
                CallToolRequestParams(name="ProbeStartRenderRequest", arguments={"frame": 7}),
            )

        assert result.is_error is False
        assert seen["payload"] == ProbeStartRenderRequest(frame=7)

    @pytest.mark.asyncio
    async def test_library_endpoint_rejects_engine_tools(self) -> None:
        result = await call_tool(
            _context_for(f"/mcp/libraries/{_PROBE_LIBRARY_SLUG}/"),
            CallToolRequestParams(name="ListRegisteredLibrariesRequest", arguments={}),
        )

        assert result.is_error is True
        assert "Unsupported tool: ListRegisteredLibrariesRequest" in result.content[0].text  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_engine_tool_wins_a_name_clash(self) -> None:
        clashing = MCPTool(ProbeStartRenderRequest)
        tools = {"ListRegisteredLibrariesRequest": clashing}

        with patch.object(mcp_module, "_collect_library_tools", return_value=tools):
            served = await _tools_for_request(_context_for("/mcp/", b"libraries=anything"))

        assert served["ListRegisteredLibrariesRequest"] is ENGINE_TOOLS["ListRegisteredLibrariesRequest"]
