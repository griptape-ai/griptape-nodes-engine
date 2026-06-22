import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    TextContent,
    Tool,
)
from pydantic import TypeAdapter
from starlette.types import Receive, Scope, Send

from griptape_nodes.retained_mode.events.base_events import (
    EventResultFailure,
    EventResultSuccess,
    RequestPayload,
)
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigValueRequest,
    GetWorkspaceRequest,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
)
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    GetWorkflowContextRequest,
    SetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ResolveNodeRequest,
    StartFlowFromNodeRequest,
    StartFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    AutoLayoutFlowRequest,
    CreateFlowRequest,
    DeleteFlowRequest,
    ListFlowsInCurrentContextRequest,
    ListNodesInFlowRequest,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    GetEngineSourceInfoRequest,
    GetLibrarySourceInfoRequest,
    ListCategoriesInLibraryRequest,
    ListNodeTypesInLibraryRequest,
    ListRegisteredLibrariesRequest,
    RegisterSandboxNodeFromSourceRequest,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    DeleteNodeRequest,
    GetAllNodeInfoRequest,
    GetNodeMetadataRequest,
    GetNodeResolutionStateRequest,
    ListParametersOnNodeRequest,
    ResetNodeToDefaultsRequest,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
)
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    RenameObjectRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    GetConnectionsForParameterRequest,
    GetParameterDetailsRequest,
    GetParameterValueRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ListAllWorkflowsRequest,
    RunWorkflowWithCurrentStateRequest,
    SaveWorkflowRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

SUPPORTED_REQUEST_EVENTS: dict[str, type[RequestPayload]] = {
    # Workflows
    "RunWorkflowWithCurrentStateRequest": RunWorkflowWithCurrentStateRequest,
    "ListAllWorkflowsRequest": ListAllWorkflowsRequest,
    # Workflow context
    "SetWorkflowContextRequest": SetWorkflowContextRequest,
    "GetWorkflowContextRequest": GetWorkflowContextRequest,
    "EnsureWorkflowAndFlowRequest": EnsureWorkflowAndFlowRequest,
    # Libraries
    "ListRegisteredLibrariesRequest": ListRegisteredLibrariesRequest,
    "ListNodeTypesInLibraryRequest": ListNodeTypesInLibraryRequest,
    "ListCategoriesInLibraryRequest": ListCategoriesInLibraryRequest,
    "RegisterSandboxNodeFromSourceRequest": RegisterSandboxNodeFromSourceRequest,
    "DescribeNodeTypeRequest": DescribeNodeTypeRequest,
    # Configuration
    "GetConfigValueRequest": GetConfigValueRequest,
    "GetWorkspaceRequest": GetWorkspaceRequest,
    # Execution
    "ResolveNodeRequest": ResolveNodeRequest,
    "ExecuteNodeRequest": ExecuteNodeRequest,
    "StartFlowRequest": StartFlowRequest,
    "StartFlowFromNodeRequest": StartFlowFromNodeRequest,
    # Flows
    "CreateFlowRequest": CreateFlowRequest,
    "DeleteFlowRequest": DeleteFlowRequest,
    "ListFlowsInCurrentContextRequest": ListFlowsInCurrentContextRequest,
    # Nodes
    "CreateNodeRequest": CreateNodeRequest,
    "DeleteNodeRequest": DeleteNodeRequest,
    "ListNodesInFlowRequest": ListNodesInFlowRequest,
    "AutoLayoutFlowRequest": AutoLayoutFlowRequest,
    "GetNodeResolutionStateRequest": GetNodeResolutionStateRequest,
    "GetNodeMetadataRequest": GetNodeMetadataRequest,
    "SetNodeMetadataRequest": SetNodeMetadataRequest,
    "ResetNodeToDefaultsRequest": ResetNodeToDefaultsRequest,
    "SetLockNodeStateRequest": SetLockNodeStateRequest,
    # Objects
    "RenameObjectRequest": RenameObjectRequest,
    "ClearAllObjectStateRequest": ClearAllObjectStateRequest,
    # Connections
    "CreateConnectionRequest": CreateConnectionRequest,
    "DeleteConnectionRequest": DeleteConnectionRequest,
    "ListConnectionsForNodeRequest": ListConnectionsForNodeRequest,
    # Parameters
    "ListParametersOnNodeRequest": ListParametersOnNodeRequest,
    "GetParameterValueRequest": GetParameterValueRequest,
    "SetParameterValueRequest": SetParameterValueRequest,
    "GetParameterDetailsRequest": GetParameterDetailsRequest,
    "GetConnectionsForParameterRequest": GetConnectionsForParameterRequest,
    # Expander-style ParameterList parameters (e.g. input_images on OpenAiImageGeneration) require
    # a slot to be created before a connection can be made. AddParameterToNodeRequest creates that
    # slot and returns its UUID name, which can then be used as the target of CreateConnectionRequest.
    "AddParameterToNodeRequest": AddParameterToNodeRequest,
    # Batch node info (metadata + state + connections + params in one call)
    "GetAllNodeInfoRequest": GetAllNodeInfoRequest,
    # Workflow persistence
    "SaveWorkflowRequest": SaveWorkflowRequest,
    # Library / engine source discovery (read-only)
    "GetLibrarySourceInfoRequest": GetLibrarySourceInfoRequest,
    "GetEngineSourceInfoRequest": GetEngineSourceInfoRequest,
}

# Synthetic MCP tool name for the batch envelope. Not a request payload (and so not a member of
# SUPPORTED_REQUEST_EVENTS); the call_tool dispatch special-cases it onto _dispatch_batch_to_engine.
EVENT_REQUEST_BATCH_TOOL_NAME = "EventRequestBatch"
EVENT_REQUEST_BATCH_DESCRIPTION = (
    "Send N requests in a single round trip and gather their responses.\n\n"
    "Each inner request is validated and dispatched as if it were its own MCP tool call, but\n"
    "they all travel as a single transport frame to the engine, so this collapses an N-call\n"
    "build phase down to one round trip. Use it whenever you already know the shape of what\n"
    "you want to build (e.g. several CreateNodeRequest + SetParameterValueRequest +\n"
    "CreateConnectionRequest calls in a row).\n\n"
    "Args:\n"
    "    requests: ordered list of inner calls. Each entry is\n"
    "        {request_type: <name of a supported tool>, request: <that tool's argument object>}.\n"
    "    timeout_ms: overall timeout for the whole batch in milliseconds. Defaults to\n"
    "        30000 ms per inner request, capped at 300000 ms.\n\n"
    "Returns:\n"
    "    list of trimmed responses in submission order. Each entry has the same shape as a\n"
    "    single tool call ({ok, details, ...payload fields}). Failures appear as\n"
    "    {ok: false, details: ...} in their slot rather than aborting the rest of the batch.\n"
)
# Per-inner-request timeout used when the caller does not pass timeout_ms. Mirrors the timeout the
# single-request path applies (see call_tool below) so a batch of one behaves identically.
_BATCH_PER_REQUEST_TIMEOUT_MS = 30000
# Hard ceiling for an auto-computed batch timeout. Long enough to accommodate a large build phase
# without letting a runaway batch hold the connection open indefinitely.
_BATCH_MAX_AUTO_TIMEOUT_MS = 300000

GTN_MCP_SERVER_HOST = os.getenv("GTN_MCP_SERVER_HOST", "localhost")
# Port of the MCP server (where uvicorn binds). Stable by default so external MCP clients
# (Claude Desktop, Cursor, VS Code, ...) can hard-code the URL in their config files.
# Set to 0 to let the OS assign a free port; set to any other value to pin the port.
GTN_MCP_SERVER_PORT = int(os.getenv("GTN_MCP_SERVER_PORT", "8125"))
GTN_MCP_SERVER_LOG_LEVEL = os.getenv("GTN_MCP_SERVER_LOG_LEVEL", "ERROR").lower()

config_manager = ConfigManager()
secrets_manager = SecretsManager(config_manager)

mcp_server_logger = logging.getLogger("griptape_nodes_mcp_server")
mcp_server_logger.setLevel(logging.INFO)


def _summarize_result_details(result_details: object) -> str | list[dict] | None:
    """Collapse the engine's nested result_details payload into something terse.

    The engine emits `result_details` as a dict wrapping a list of ResultDetail entries,
    e.g. ``{"result_details": [{"level": 10, "message": "..."}, ...]}``. For the MCP
    surface we only really need the messages, joined on newlines. Anything we do not
    recognize is returned as-is so we never hide information we did not intend to hide.
    """
    if result_details is None:
        return None
    if isinstance(result_details, str):
        return result_details
    if isinstance(result_details, dict):
        inner = result_details.get("result_details")
        if isinstance(inner, list):
            messages = [entry.get("message", "") for entry in inner if isinstance(entry, dict)]
            joined = "\n".join(message for message in messages if message)
            if joined:
                return joined
            return inner
    return result_details  # type: ignore[return-value]


def _trim_response(result: dict) -> dict:
    """Strip envelope noise from an engine response before we hand it back to the MCP client.

    The raw response wraps the real payload in engine/session/routing metadata and an echoed
    request. Agents only need to know whether the call succeeded and the payload fields the
    handler produced, so we surface a success discriminator, a terse `details` string, and the
    rest of the inner `result` object.
    """
    inner = dict(result.get("result") or {})
    result_type = result.get("result_type", "")
    details = _summarize_result_details(inner.pop("result_details", None))

    trimmed: dict = {"ok": result_type.endswith("Success")}
    if details is not None:
        trimmed["details"] = details
    trimmed.update(inner)
    return trimmed


def _event_request_batch_input_schema() -> dict[str, Any]:
    """JSON schema for the synthetic EventRequestBatch tool.

    The `request` payload of each inner entry is left as a free-form object because JSON Schema
    cannot easily express "validate this against the dataclass selected by request_type"; the
    server-side handler validates each payload by instantiating the matching RequestPayload
    class, mirroring the single-request dispatch path.
    """
    return {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "request_type": {
                            "type": "string",
                            "enum": sorted(SUPPORTED_REQUEST_EVENTS),
                            "description": "Name of one of the supported single-request tools (e.g. CreateNodeRequest).",
                        },
                        "request": {
                            "type": "object",
                            "description": "Argument object that tool would accept individually.",
                        },
                    },
                    "required": ["request_type", "request"],
                },
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": "Overall timeout for the batch in milliseconds.",
            },
        },
        "required": ["requests"],
    }


def _build_batch_pairs(raw_requests: object) -> list[tuple[str, dict[str, Any]]]:
    """Validate the inner requests array and return (request_type, payload_dict) pairs.

    Mirrors the single-request dispatch: each inner request is validated by instantiating the
    matching RequestPayload class so missing required fields and unknown kwargs fail fast,
    before anything reaches the wire.
    """
    if not isinstance(raw_requests, list):
        msg = "Attempted to dispatch EventRequestBatch. Failed because 'requests' must be a list."
        raise TypeError(msg)
    if not raw_requests:
        msg = "Attempted to dispatch EventRequestBatch. Failed because 'requests' was empty."
        raise ValueError(msg)

    pairs: list[tuple[str, dict[str, Any]]] = []
    for index, entry in enumerate(raw_requests):
        if not isinstance(entry, dict):
            msg = f"Attempted to dispatch EventRequestBatch entry {index}. Failed because the entry was not an object."
            raise TypeError(msg)
        request_type = entry.get("request_type")
        if not isinstance(request_type, str) or request_type not in SUPPORTED_REQUEST_EVENTS:
            msg = (
                f"Attempted to dispatch EventRequestBatch entry {index}. "
                f"Failed because request_type {request_type!r} is not a supported tool."
            )
            raise ValueError(msg)
        inner = entry.get("request", {})
        if not isinstance(inner, dict):
            msg = (
                f"Attempted to dispatch EventRequestBatch entry {index} ({request_type}). "
                f"Failed because 'request' must be an object."
            )
            raise TypeError(msg)

        payload_cls = SUPPORTED_REQUEST_EVENTS[request_type]
        try:
            payload_obj = payload_cls(**inner)
        except TypeError as exc:
            msg = (
                f"Attempted to construct {request_type} for EventRequestBatch entry {index}. "
                f"Failed with arguments {inner!r} because of {exc}."
            )
            raise ValueError(msg) from exc

        pairs.append((request_type, dict(payload_obj.__dict__)))

    return pairs


def _resolve_batch_timeout_ms(override: object, num_requests: int) -> int:
    """Return the timeout_ms to apply to a batch, validating any caller override.

    Without an override, scales the per-request timeout linearly with batch size and clamps to a
    ceiling so a malformed call cannot hold the connection open indefinitely.
    """
    if override is None:
        return min(_BATCH_PER_REQUEST_TIMEOUT_MS * num_requests, _BATCH_MAX_AUTO_TIMEOUT_MS)
    # bool is a subclass of int; reject explicitly so True does not get treated as 1ms.
    if not isinstance(override, int) or isinstance(override, bool):
        msg = (
            "Attempted to dispatch EventRequestBatch. "
            f"Failed because timeout_ms must be a positive integer, got {override!r}."
        )
        raise TypeError(msg)
    if override <= 0:
        msg = (
            "Attempted to dispatch EventRequestBatch. "
            f"Failed because timeout_ms must be a positive integer, got {override!r}."
        )
        raise ValueError(msg)
    return override


def _trim_batch_results(raw_results: list[Any]) -> list[dict[str, Any]]:
    """Trim each inner response, mapping exceptions returned by request_batch to ok=false slots."""
    trimmed: list[dict[str, Any]] = []
    for raw in raw_results:
        if isinstance(raw, BaseException):
            trimmed.append({"ok": False, "details": str(raw)})
        else:
            trimmed.append(_trim_response(raw))
    return trimmed


async def _handle_request_on_engine_loop(request_payload: RequestPayload) -> dict[str, Any]:
    """Handle a request on the engine loop and serialize the result to the wire shape.

    Must be scheduled onto the engine's event loop (see _dispatch_to_engine). GriptapeNodes.
    ahandle_request is the same in-memory entry point the engine's own inbound path uses: it
    dispatches to the assigned manager and broadcasts the result so connected clients (e.g. the
    editor) still observe the change. The returned ResultPayload is wrapped back into an
    EventResult purely to reuse its wire serializer, producing the same dict shape the WebSocket
    transport delivered, which _trim_response already knows how to collapse.
    """
    result_payload = await GriptapeNodes.ahandle_request(request_payload)
    if result_payload.succeeded():
        result_event: EventResultSuccess | EventResultFailure = EventResultSuccess(
            request=request_payload, result=result_payload
        )
    else:
        result_event = EventResultFailure(request=request_payload, result=result_payload)
    return json.loads(result_event.json())


async def _dispatch_to_engine(request_payload: RequestPayload, timeout_ms: int | None = None) -> dict[str, Any]:
    """Dispatch a request from the MCP server's loop onto the engine loop and await the result.

    The MCP server runs uvicorn on its own event loop in a daemon thread, so `call_tool` is not
    on the engine's loop and cannot simply `await ahandle_request` (that would run the handler
    on uvicorn's loop, concurrent with the engine instead of serialized onto it). run_coroutine_
    threadsafe schedules the work on the engine loop; wrap_future binds the cross-thread future
    back to the MCP server's loop so it can be awaited and timed out here without blocking.

    The wrapped future is wrapped again in ``asyncio.shield`` so that a client-side timeout
    (the ``wait_for`` below) or a sibling cancellation in a batch ``gather`` cancels only this
    coroutine's wait, never the engine-side operation already running on the engine loop. A
    bare ``wait_for(response_future)`` would, on timeout, cancel ``response_future`` and thereby
    cancel the engine coroutine mid-flight; for a multi-node resolve that strands the node it
    was executing in ``RESOLVING`` forever while the orphaned execution task keeps running
    (griptape-nodes-engine#4883). The pre-in-process WebSocket transport let the engine run a
    request to completion even after the client stopped waiting, and shielding preserves that
    contract: the caller still sees a ``TimeoutError`` and can poll for state afterwards.
    """
    engine_loop = GriptapeNodes.EventManager().event_loop
    if engine_loop is None:
        msg = (
            "Attempted to dispatch an MCP request to the engine. "
            "Failed because the engine event loop is not running yet."
        )
        raise RuntimeError(msg)
    response_future = asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(_handle_request_on_engine_loop(request_payload), engine_loop)
    )
    if timeout_ms:
        return await asyncio.wait_for(asyncio.shield(response_future), timeout=timeout_ms / 1000)
    return await asyncio.shield(response_future)


async def _dispatch_batch_to_engine(pairs: list[tuple[str, dict[str, Any]]], timeout_ms: int) -> list[Any]:
    """Dispatch a batch of (request_type, payload) pairs concurrently onto the engine loop.

    Mirrors the single-request path but gathers the per-request futures. Failures are returned
    in their slot (return_exceptions semantics) so one bad inner request does not abort the rest;
    _trim_batch_results maps those exceptions to ok=false responses.
    """
    coros = [_dispatch_to_engine(SUPPORTED_REQUEST_EVENTS[request_type](**payload)) for request_type, payload in pairs]
    gather = asyncio.gather(*coros, return_exceptions=True)
    return await asyncio.wait_for(gather, timeout=timeout_ms / 1000)


def start_mcp_server(sock: socket.socket) -> None:
    """Synchronous version of main entry point for the Griptape Nodes MCP server.

    The socket should already be bound to the desired address and port before calling
    this function. Using a pre-bound socket avoids race conditions when discovering
    the actual port assigned by the OS.
    """
    bound_host, bound_port = sock.getsockname()[:2]
    mcp_server_logger.info("MCP server listening at http://%s:%d/mcp/", bound_host, bound_port)

    app = Server("mcp-gtn")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        single_tools = [
            Tool(name=event.__name__, description=event.__doc__, inputSchema=TypeAdapter(event).json_schema())
            for (name, event) in SUPPORTED_REQUEST_EVENTS.items()
        ]
        batch_tool = Tool(
            name=EVENT_REQUEST_BATCH_TOOL_NAME,
            description=EVENT_REQUEST_BATCH_DESCRIPTION,
            inputSchema=_event_request_batch_input_schema(),
        )
        return [*single_tools, batch_tool]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == EVENT_REQUEST_BATCH_TOOL_NAME:
            pairs = _build_batch_pairs(arguments.get("requests"))
            timeout_ms = _resolve_batch_timeout_ms(arguments.get("timeout_ms"), len(pairs))
            raw_results = await _dispatch_batch_to_engine(pairs, timeout_ms)
            mcp_server_logger.debug("Got %d batch results", len(raw_results))
            return [TextContent(type="text", text=json.dumps(_trim_batch_results(raw_results)))]

        if name not in SUPPORTED_REQUEST_EVENTS:
            msg = f"Unsupported tool: {name}"
            raise ValueError(msg)

        request_payload = SUPPORTED_REQUEST_EVENTS[name](**arguments)
        result = await _dispatch_to_engine(request_payload, timeout_ms=30000)
        mcp_server_logger.debug("Got result: %s", result)

        return [TextContent(type="text", text=json.dumps(_trim_response(result)))]

    # Create the session manager with our app and event store
    session_manager = StreamableHTTPSessionManager(
        app=app,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run the StreamableHTTP session manager for the lifetime of the FastAPI app.

        Requests are dispatched straight into the engine's event loop (see _dispatch_to_engine),
        so there is no transport client to set up or tear down here.
        """
        async with session_manager.run():
            mcp_server_logger.debug("GTN MCP server started with StreamableHTTP session manager!")
            try:
                yield
            finally:
                mcp_server_logger.debug("GTN MCP server shutting down...")

    mcp_server_app = FastAPI(lifespan=lifespan)

    # ASGI handler for streamable HTTP connections
    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    mcp_server_app.mount("/mcp", app=handle_streamable_http)

    try:
        config = uvicorn.Config(mcp_server_app, log_config=None, log_level=GTN_MCP_SERVER_LOG_LEVEL)
        server = uvicorn.Server(config)
        asyncio.run(server.serve(sockets=[sock]))
    except Exception as e:
        mcp_server_logger.error("MCP server failed: %s", e)
        raise
