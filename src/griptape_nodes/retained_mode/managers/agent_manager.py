"""Chat-sidebar agent manager backed by the Pydantic AI harness.

This manager owns:

  * the lifecycle of a per-process :class:`PydanticAgentRunner` that talks to
    Griptape Cloud through its OpenAI-compatible Chat Completions endpoint,
  * the local thread storage backend that persists Pydantic AI message
    history,
  * the existing engine-bundled MCP server (started here as a background
    thread, just like before),
  * the same request handlers the chat sidebar already calls
    (``RunAgentRequest``, ``ConfigureAgentRequest``, the thread CRUD set,
    ``GetConversationMemoryRequest``, ``ListAgentModelsRequest``).

The Griptape ``Agent`` and the JSON-output parsing dance it required are gone.
Streaming tokens come straight off Pydantic AI's text deltas via the runner's
``token_sink`` callback and land on the UI as ``AgentStreamEvent`` payloads.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import textwrap
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from pydantic_ai.messages import BinaryContent, ModelMessagesTypeAdapter
from pydantic_ai.usage import UsageLimits
from xdg_base_dirs import xdg_data_home

from griptape_nodes.agents.pydantic_ai.image_tools import GRIPTAPE_CLOUD_BASE_URL, ImageGenerationToolsetConfig
from griptape_nodes.agents.pydantic_ai.mcp_servers import mcp_server_from_config, streamable_http_local
from griptape_nodes.agents.pydantic_ai.runner import (
    PydanticAgentRunner,
    RunEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
)
from griptape_nodes.drivers.cloud_models import (
    DEPRECATED_MODELS,
    IMAGE_DEPRECATED_MODELS,
    IMAGE_MODEL_CHOICES,
    MODEL_CHOICES,
    PROVIDER_PRESETS,
)
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver
from griptape_nodes.retained_mode.events.agent_events import (
    AgentStreamEvent,
    AgentThinkingEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    ArchiveThreadRequest,
    ArchiveThreadResultFailure,
    ArchiveThreadResultSuccess,
    CancelAgentRequest,
    CancelAgentResultFailure,
    CancelAgentResultSuccess,
    ConfigureAgentRequest,
    ConfigureAgentResultFailure,
    ConfigureAgentResultSuccess,
    CreateAgentProviderRequest,
    CreateAgentProviderResultFailure,
    CreateAgentProviderResultSuccess,
    CreateThreadRequest,
    CreateThreadResultFailure,
    CreateThreadResultSuccess,
    DeleteAgentProviderRequest,
    DeleteAgentProviderResultFailure,
    DeleteAgentProviderResultSuccess,
    DeleteThreadRequest,
    DeleteThreadResultFailure,
    DeleteThreadResultSuccess,
    GetAgentConfigRequest,
    GetAgentConfigResultSuccess,
    GetConversationMemoryRequest,
    GetConversationMemoryResultFailure,
    GetConversationMemoryResultSuccess,
    ListAgentModelsRequest,
    ListAgentModelsResultSuccess,
    ListAgentProvidersRequest,
    ListAgentProvidersResultSuccess,
    ListProviderModelsRequest,
    ListProviderModelsResultFailure,
    ListProviderModelsResultSuccess,
    ListThreadsRequest,
    ListThreadsResultFailure,
    ListThreadsResultSuccess,
    RenameThreadRequest,
    RenameThreadResultFailure,
    RenameThreadResultSuccess,
    RunAgentRequest,
    RunAgentRequestArtifact,
    RunAgentResultFailure,
    RunAgentResultSuccess,
    UnarchiveThreadRequest,
    UnarchiveThreadResultFailure,
    UnarchiveThreadResultSuccess,
    UpdateAgentProviderRequest,
    UpdateAgentProviderResultFailure,
    UpdateAgentProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete, ConfigChanged
from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent, ResultPayload
from griptape_nodes.retained_mode.events.mcp_events import (
    GetEnabledMCPServersRequest,
    GetEnabledMCPServersResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
from griptape_nodes.servers import bind_free_socket
from griptape_nodes.servers.mcp import GTN_MCP_SERVER_HOST, GTN_MCP_SERVER_PORT, start_mcp_server

if TYPE_CHECKING:
    from pydantic_ai.messages import UserContent
    from pydantic_ai.toolsets import AbstractToolset

    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager


logger = logging.getLogger("griptape_nodes")

API_KEY_ENV_VAR = "GT_CLOUD_API_KEY"

# Valid type values for provider configs (derived from PROVIDER_PRESETS so it
# stays in sync automatically when new presets are added).
_VALID_PROVIDER_TYPES: frozenset[str] = frozenset(p["id"] for p in PROVIDER_PRESETS)

# The built-in provider that is always present and may never be deleted.
_PROTECTED_PROVIDER_NAME = "griptape_cloud"

config_manager = ConfigManager()
secrets_manager = SecretsManager(config_manager)


_AGENT_INSTRUCTIONS_BASE = (
    "You are a coding assistant embedded in Griptape Nodes. You operate by calling tools.\n\n"
    "Tools available to you:\n"
    "  - GriptapeNodes MCP tools (prefixed `GriptapeNodes_`). Use these to interact with the "
    "engine: list libraries and node types, create nodes, set parameter values, wire "
    "connections, save and run workflows.\n"
    "{image_tool_line}"
    "  - Additional MCP tools may be available, each prefixed with its server name.\n\n"
    "Behavior rules (these are non-negotiable):\n"
    "  1. NEVER respond with only a plan or a description of what you intend to do. If a "
    "     task requires tool work, call the relevant tools in the SAME turn as your "
    "     acknowledgment. A response of the form 'I'll do X' with no tool calls is wrong.\n"
    "  2. When the user asks you to build, create, modify, inspect, or run something, "
    "     start with discovery tool calls (e.g. GriptapeNodes_ListRegisteredLibrariesRequest, "
    "     GriptapeNodes_ListNodeTypesInLibraryRequest) before doing anything that "
    "     mutates state.\n"
    "  3. Make multiple tool calls in parallel when they don't depend on each other.\n"
    "  4. Only after you have actually completed the user's task should you produce a final "
    "     text response. That final response should be a short summary of what you did, "
    "     including the names of any nodes you created or changed.\n"
)

_IMAGE_TOOL_INSTRUCTION = (
    "  - generate_image: turn a text prompt into an image via Griptape Cloud. The chat UI "
    "displays the generated image inline automatically, so briefly describe what you made and "
    "do NOT paste the returned URL or markdown image syntax into your reply.\n"
)


def _build_agent_instructions(*, include_image_tool: bool) -> str:
    image_tool_line = _IMAGE_TOOL_INSTRUCTION if include_image_tool else ""
    return _AGENT_INSTRUCTIONS_BASE.format(image_tool_line=image_tool_line)


# Cap each chat-sidebar turn so a runaway loop can't burn through credits or
# wedge the conversation. The numbers are deliberately generous: 60 model
# requests is enough for a complex multi-tool task while still protecting the
# user from a tool-call loop.
DEFAULT_AGENT_USAGE_LIMITS = UsageLimits(request_limit=60)

# Bound how long we wait when downloading an attached image server-side before
# inlining it for the model, and the media type to assume when neither the
# response header nor the URL extension identifies one.
_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_DEFAULT_IMAGE_MEDIA_TYPE = "image/png"


@dataclass
class _ActiveRun:
    """Handle to an in-flight agent run, used to deliver cancellation.

    ``cancel_event`` belongs to ``loop`` (the loop the run awaits on). A
    ``CancelAgentRequest`` may be handled on a different loop (skip-the-line
    requests run on the websocket loop), so the event is always set via
    ``loop.call_soon_threadsafe`` rather than touched directly.
    """

    cancel_event: asyncio.Event
    loop: asyncio.AbstractEventLoop


class AgentManager:
    """Owns the chat-sidebar agent runner and the engine-bundled MCP server."""

    def __init__(self, static_files_manager: StaticFilesManager, event_manager: EventManager | None = None) -> None:
        self.static_files_manager = static_files_manager
        self._mcp_server_port = GTN_MCP_SERVER_PORT

        self._providers: list[dict] = []
        self._active_provider_name: str = _PROTECTED_PROVIDER_NAME
        self._load_providers_from_config()

        self._image_model_name: str = IMAGE_MODEL_CHOICES[0] if IMAGE_MODEL_CHOICES else "gpt-image-1-mini"
        self._system_prompt_extra: str = config_manager.get_config_value("agent.system_prompt", default="")

        self._threads_dir: Path = xdg_data_home() / "griptape_nodes" / "threads"
        self._thread_storage: LocalThreadStorageDriver = LocalThreadStorageDriver(
            self._threads_dir, config_manager, secrets_manager
        )

        # Cache one runner per (provider-type, model, image-model, base-url, api-key, mcp-set).
        self._runner_cache: dict[tuple[str, str, str, str, str, tuple[str, ...]], PydanticAgentRunner] = {}

        # Cancel handles for in-flight runs, keyed by thread_id.
        self._active_runs: dict[str, _ActiveRun] = {}

        if event_manager is not None:
            event_manager.assign_manager_to_request_type(RunAgentRequest, self.on_handle_run_agent_request)
            event_manager.assign_manager_to_request_type(CancelAgentRequest, self.on_handle_cancel_agent_request)
            event_manager.assign_manager_to_request_type(ConfigureAgentRequest, self.on_handle_configure_agent_request)
            event_manager.assign_manager_to_request_type(
                GetConversationMemoryRequest, self.on_handle_get_conversation_memory_request
            )
            event_manager.assign_manager_to_request_type(CreateThreadRequest, self.on_handle_create_thread_request)
            event_manager.assign_manager_to_request_type(ListThreadsRequest, self.on_handle_list_threads_request)
            event_manager.assign_manager_to_request_type(DeleteThreadRequest, self.on_handle_delete_thread_request)
            event_manager.assign_manager_to_request_type(RenameThreadRequest, self.on_handle_rename_thread_request)
            event_manager.assign_manager_to_request_type(ArchiveThreadRequest, self.on_handle_archive_thread_request)
            event_manager.assign_manager_to_request_type(
                UnarchiveThreadRequest, self.on_handle_unarchive_thread_request
            )
            event_manager.assign_manager_to_request_type(
                ListAgentModelsRequest, self.on_handle_list_agent_models_request
            )
            event_manager.assign_manager_to_request_type(GetAgentConfigRequest, self.on_handle_get_agent_config_request)
            event_manager.assign_manager_to_request_type(
                ListProviderModelsRequest, self.on_handle_list_provider_models_request
            )
            event_manager.assign_manager_to_request_type(
                ListAgentProvidersRequest, self.on_handle_list_agent_providers_request
            )
            event_manager.assign_manager_to_request_type(
                CreateAgentProviderRequest, self.on_handle_create_agent_provider_request
            )
            event_manager.assign_manager_to_request_type(
                UpdateAgentProviderRequest, self.on_handle_update_agent_provider_request
            )
            event_manager.assign_manager_to_request_type(
                DeleteAgentProviderRequest, self.on_handle_delete_agent_provider_request
            )
            event_manager.add_listener_to_app_event(
                AppInitializationComplete,
                self.on_app_initialization_complete,
            )
            event_manager.add_listener_to_app_event(
                ConfigChanged,
                self._on_config_changed,
            )

    def on_app_initialization_complete(self, _payload: AppInitializationComplete) -> None:
        sock = bind_free_socket(GTN_MCP_SERVER_HOST, GTN_MCP_SERVER_PORT)
        self._mcp_server_port = sock.getsockname()[1]
        threading.Thread(target=start_mcp_server, args=(sock,), daemon=True, name="mcp-server").start()

    def _get_provider(self, name: str | None) -> dict:
        """Return the provider dict for name, falling back to the active provider."""
        lookup = name or self._active_provider_name
        for p in self._providers:
            if p.get("name") == lookup:
                return p
        for p in self._providers:
            if p.get("name") == _PROTECTED_PROVIDER_NAME:
                return p
        default_model = MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o"
        return {"name": _PROTECTED_PROVIDER_NAME, "type": _PROTECTED_PROVIDER_NAME, "model": default_model}

    def _on_config_changed(self, event: ConfigChanged) -> None:
        _provider_keys = ("agent.providers", "agent.active_provider", "agent.griptape_cloud_model", "agent", "")
        if event.key not in ("agent.system_prompt", *_provider_keys):
            return
        new_value = config_manager.get_config_value("agent.system_prompt", default="")
        if new_value != self._system_prompt_extra:
            self._system_prompt_extra = new_value
            self._runner_cache.clear()
        if event.key in _provider_keys:
            self._load_providers_from_config()
            self._runner_cache.clear()

    async def on_handle_run_agent_request(self, request: RunAgentRequest) -> ResultPayload:
        try:
            return await self._run_agent(request)
        except Exception as e:
            err_msg = f"Error running agent: {e}"
            logger.exception(err_msg)
            return RunAgentResultFailure(error={"message": str(e)}, result_details=err_msg)

    async def _run_agent(self, request: RunAgentRequest) -> ResultPayload:
        thread_id = self._validate_thread_for_run(request.thread_id)
        is_first_run = len(self._thread_storage.load_history(thread_id)) == 0

        runner = self._build_runner(
            request.additional_mcp_servers,
            provider_name=request.provider_name,
            model_name=request.model_name,
        )
        prompt = await _compose_prompt(request.input, request.url_artifacts)

        event_manager = GriptapeNodes.EventManager()

        def emit(event: RunEvent) -> None:
            payload = _run_event_to_payload(event)
            if payload is None:
                return
            event_manager.put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=payload),
                ),
            )

        cancel_event = asyncio.Event()
        self._active_runs[thread_id] = _ActiveRun(cancel_event=cancel_event, loop=asyncio.get_running_loop())
        try:
            result = await runner.run(prompt, thread_id=thread_id, event_sink=emit, cancel_event=cancel_event)
        finally:
            # Only drop our own entry; a newer run for the same thread may have
            # replaced it (shouldn't happen for the chat sidebar, but stay safe).
            if (active := self._active_runs.get(thread_id)) is not None and active.cancel_event is cancel_event:
                del self._active_runs[thread_id]

        # A first run creates the thread; title it from the input even when the
        # turn is cancelled, so a quick send-then-cancel doesn't leave a
        # titleless orphan thread in the listing.
        if is_first_run:
            self._thread_storage.update_thread_metadata(
                result.thread_id, title=textwrap.shorten(request.input, width=50, placeholder="...")
            )

        if result.cancelled:
            logger.info("Agent run for thread %s cancelled by request.", result.thread_id)
            return RunAgentResultSuccess(
                output={
                    "text": result.output,
                    "message_count": result.message_count,
                    "cancelled": True,
                    "generated_image_urls": result.image_urls,
                },
                thread_id=result.thread_id,
                result_details="Agent run cancelled.",
            )

        return RunAgentResultSuccess(
            output={
                "text": result.output,
                "message_count": result.message_count,
                "cancelled": False,
                "generated_image_urls": result.image_urls,
            },
            thread_id=result.thread_id,
            result_details="Agent execution completed successfully.",
        )

    def on_handle_cancel_agent_request(self, request: CancelAgentRequest) -> ResultPayload:
        """Signal cooperative cancellation to the in-flight run for a thread.

        Idempotent: returns success even when no run is active so the UI can fire
        cancel without first checking run state. ``was_running`` distinguishes
        the two cases.
        """
        try:
            active = self._active_runs.get(request.thread_id)
            if active is None:
                return CancelAgentResultSuccess(
                    thread_id=request.thread_id,
                    was_running=False,
                    result_details=f"No active agent run for thread {request.thread_id}.",
                )
            # The run awaits on active.loop, which may differ from the loop handling
            # this (skip-the-line) request; asyncio.Event is not thread-safe, so hop.
            active.loop.call_soon_threadsafe(active.cancel_event.set)
            return CancelAgentResultSuccess(
                thread_id=request.thread_id,
                was_running=True,
                result_details=f"Cancellation signalled for thread {request.thread_id}.",
            )
        except Exception as e:
            details = f"Error cancelling agent run: {e}"
            logger.exception(details)
            return CancelAgentResultFailure(result_details=details)

    def on_handle_create_thread_request(self, request: CreateThreadRequest) -> ResultPayload:
        try:
            thread_id, meta = self._thread_storage.create_thread(title=request.title, local_id=request.local_id)
            return CreateThreadResultSuccess(
                thread_id=thread_id,
                title=meta.get("title"),
                created_at=meta["created_at"],
                updated_at=meta["updated_at"],
                result_details="Thread created successfully.",
            )
        except Exception as e:
            details = f"Error creating thread: {e}"
            logger.exception(details)
            return CreateThreadResultFailure(result_details=details)

    def on_handle_list_threads_request(self, _: ListThreadsRequest) -> ResultPayload:
        try:
            threads = self._thread_storage.list_threads()
            return ListThreadsResultSuccess(threads=threads, result_details="Threads retrieved successfully.")
        except Exception as e:
            details = f"Error listing threads: {e}"
            logger.exception(details)
            return ListThreadsResultFailure(result_details=details)

    def on_handle_delete_thread_request(self, request: DeleteThreadRequest) -> ResultPayload:
        try:
            self._thread_storage.delete_thread(request.thread_id)
            return DeleteThreadResultSuccess(thread_id=request.thread_id, result_details="Thread deleted successfully.")
        except ValueError as e:
            details = str(e)
            logger.error(details)
            return DeleteThreadResultFailure(result_details=details)
        except Exception as e:
            details = f"Error deleting thread: {e}"
            logger.exception(details)
            return DeleteThreadResultFailure(result_details=details)

    def on_handle_rename_thread_request(self, request: RenameThreadRequest) -> ResultPayload:
        try:
            if not self._thread_storage.thread_exists(request.thread_id):
                details = f"Thread {request.thread_id} not found"
                logger.error(details)
                return RenameThreadResultFailure(result_details=details)

            updated_meta = self._thread_storage.update_thread_metadata(request.thread_id, title=request.new_title)
            return RenameThreadResultSuccess(
                thread_id=request.thread_id,
                title=updated_meta["title"],
                updated_at=updated_meta["updated_at"],
                result_details="Thread renamed successfully.",
            )
        except Exception as e:
            details = f"Error renaming thread: {e}"
            logger.exception(details)
            return RenameThreadResultFailure(result_details=details)

    def on_handle_archive_thread_request(self, request: ArchiveThreadRequest) -> ResultPayload:
        try:
            if not self._thread_storage.thread_exists(request.thread_id):
                details = f"Thread {request.thread_id} not found"
                logger.error(details)
                return ArchiveThreadResultFailure(result_details=details)

            meta = self._thread_storage.get_thread_metadata(request.thread_id)
            if meta.get("archived", False):
                details = f"Thread {request.thread_id} is already archived"
                logger.error(details)
                return ArchiveThreadResultFailure(result_details=details)

            updated_meta = self._thread_storage.update_thread_metadata(request.thread_id, archived=True)
            return ArchiveThreadResultSuccess(
                thread_id=request.thread_id,
                updated_at=updated_meta["updated_at"],
                result_details="Thread archived successfully.",
            )
        except Exception as e:
            details = f"Error archiving thread: {e}"
            logger.exception(details)
            return ArchiveThreadResultFailure(result_details=details)

    def on_handle_unarchive_thread_request(self, request: UnarchiveThreadRequest) -> ResultPayload:
        try:
            if not self._thread_storage.thread_exists(request.thread_id):
                details = f"Thread {request.thread_id} not found"
                logger.error(details)
                return UnarchiveThreadResultFailure(result_details=details)

            meta = self._thread_storage.get_thread_metadata(request.thread_id)
            if not meta.get("archived", False):
                details = f"Thread {request.thread_id} is not archived"
                logger.error(details)
                return UnarchiveThreadResultFailure(result_details=details)

            updated_meta = self._thread_storage.update_thread_metadata(request.thread_id, archived=False)
            return UnarchiveThreadResultSuccess(
                thread_id=request.thread_id,
                updated_at=updated_meta["updated_at"],
                result_details="Thread unarchived successfully.",
            )
        except Exception as e:
            details = f"Error unarchiving thread: {e}"
            logger.exception(details)
            return UnarchiveThreadResultFailure(result_details=details)

    def on_handle_configure_agent_request(self, request: ConfigureAgentRequest) -> ResultPayload:
        """Update agent configuration from the chat sidebar.

        Prompt driver keys honored: ``provider``, ``model``, ``base_url``,
        ``api_key``. Image generation driver key honored: ``model``. Other
        keys are accepted but ignored. Any change that affects the runner
        flushes the runner cache so the next run picks up the new settings.
        """
        try:
            changed = self._apply_prompt_driver_config(request.prompt_driver)
            if "model" in request.image_generation_driver:
                new_image_model = str(request.image_generation_driver["model"])
                if new_image_model != self._image_model_name:
                    self._image_model_name = new_image_model
                    changed = True
            if request.active_provider:
                provider_names = {p.get("name") for p in self._providers}
                if request.active_provider not in provider_names:
                    return ConfigureAgentResultFailure(
                        result_details=f"Attempted to set active provider '{request.active_provider}'. Failed because it does not exist."
                    )
                if request.active_provider != self._active_provider_name:
                    self._active_provider_name = request.active_provider
                    changed = True
            if changed:
                self._persist_providers()
                self._runner_cache.clear()
        except Exception as e:
            details = f"Error configuring agent: {e}"
            logger.exception(details)
            return ConfigureAgentResultFailure(result_details=details)
        return ConfigureAgentResultSuccess(result_details="Agent configured successfully.")

    def _apply_prompt_driver_config(self, pd: dict) -> bool:
        """Apply prompt driver config fields to the active provider, return True if any value changed."""
        gc = self._get_provider(None)
        changed = False
        for req_key, dict_key in (
            ("model", "model"),
            ("base_url", "base_url"),
            ("api_key", "api_key"),
        ):
            if req_key in pd:
                new_value = str(pd[req_key])
                if gc.get(dict_key) != new_value:
                    gc[dict_key] = new_value
                    changed = True
        return changed

    def on_handle_list_agent_models_request(self, _: ListAgentModelsRequest) -> ResultPayload:
        return ListAgentModelsResultSuccess(
            prompt_models=list(MODEL_CHOICES),
            image_models=list(IMAGE_MODEL_CHOICES),
            deprecated_models={**DEPRECATED_MODELS, **IMAGE_DEPRECATED_MODELS},
            provider_presets=list(PROVIDER_PRESETS),
            result_details="Agent model lists retrieved successfully.",
        )

    def on_handle_get_agent_config_request(self, _: GetAgentConfigRequest) -> ResultPayload:
        gc = self._get_provider(None)
        return GetAgentConfigResultSuccess(
            provider=str(gc.get("type", _PROTECTED_PROVIDER_NAME)),
            model_name=str(gc.get("model", "")),
            image_model_name=self._image_model_name,
            base_url=str(gc.get("base_url", "")),
            result_details="Agent config retrieved successfully.",
        )

    async def on_handle_list_provider_models_request(self, request: ListProviderModelsRequest) -> ResultPayload:
        try:
            if request.provider == "griptape_cloud":
                return ListProviderModelsResultSuccess(
                    models=list(MODEL_CHOICES),
                    result_details="Griptape Cloud model list retrieved.",
                )

            base_url = request.base_url.rstrip("/")
            if not base_url:
                return ListProviderModelsResultFailure(
                    result_details="Attempted to list provider models. Failed because base_url is required for non-Griptape-Cloud providers."
                )

            headers: dict[str, str] = {}
            if request.api_key:
                headers["Authorization"] = f"Bearer {request.api_key}"

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{base_url}/models", headers=headers)
                response.raise_for_status()

            data = response.json()
            models = sorted(entry["id"] for entry in data.get("data", []) if "id" in entry)
            return ListProviderModelsResultSuccess(
                models=models,
                result_details=f"Retrieved {len(models)} models from {base_url}.",
            )
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            details = f"Attempted to list models from '{request.base_url}'. Failed with: {e}"
            logger.warning(details)
            return ListProviderModelsResultFailure(result_details=details)

    def on_handle_get_conversation_memory_request(self, request: GetConversationMemoryRequest) -> ResultPayload:
        try:
            history = self._thread_storage.load_history(request.thread_id)
            messages = ModelMessagesTypeAdapter.dump_python(history, mode="json")
            return GetConversationMemoryResultSuccess(
                messages=messages,
                thread_id=request.thread_id,
                result_details="Conversation memory retrieved successfully.",
            )
        except Exception as e:
            details = f"Error getting conversation memory: {e}"
            logger.exception(details)
            return GetConversationMemoryResultFailure(result_details=details)

    def _build_runner(
        self,
        additional_mcp_servers: list[str],
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> PydanticAgentRunner:
        provider = self._get_provider(provider_name)
        provider_type = str(provider.get("type", _PROTECTED_PROVIDER_NAME))
        model_name = model_name or str(provider.get("model", MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o"))
        base_url = str(provider.get("base_url", ""))
        api_key = str(provider.get("api_key", ""))

        cache_key = (
            provider_type,
            model_name,
            self._image_model_name,
            base_url,
            api_key,
            tuple(sorted(additional_mcp_servers)),
        )
        if (cached := self._runner_cache.get(cache_key)) is not None:
            return cached

        workspace_root = Path(config_manager.workspace_path)
        mcp_servers: list[AbstractToolset[Any]] = [
            streamable_http_local(
                f"http://localhost:{self._mcp_server_port}/mcp/",
                name="GriptapeNodes",
            ),
        ]
        server_rules: list[str] = []
        for cfg in self._lookup_mcp_configs(additional_mcp_servers):
            built = mcp_server_from_config(cfg["name"], cfg)
            if built is not None:
                mcp_servers.append(built)
            rules = cfg.get("rules")
            if isinstance(rules, str) and rules.strip():
                server_rules.append(f"Rules for MCP server '{cfg['name']}':\n{rules.strip()}")

        if provider_type == _PROTECTED_PROVIDER_NAME:
            api_key = secrets_manager.get_secret(API_KEY_ENV_VAR)
            if not api_key:
                msg = f"Secret '{API_KEY_ENV_VAR}' not found"
                raise ValueError(msg)
            # Match build_griptape_cloud_model's `or` semantics: a set-but-empty
            # GT_CLOUD_BASE_URL falls back to the default rather than yielding a
            # malformed endpoint, so the chat and image paths agree.
            cloud_base_url = os.environ.get("GT_CLOUD_BASE_URL") or GRIPTAPE_CLOUD_BASE_URL
            model_base_url: str | None = cloud_base_url
            image_config: ImageGenerationToolsetConfig | None = ImageGenerationToolsetConfig(
                api_key=api_key, model=self._image_model_name, base_url=cloud_base_url
            )
        else:
            model_base_url = base_url or None
            # Image generation is Griptape Cloud-specific; disable for other providers.
            image_config = None

        runner = PydanticAgentRunner(
            model_name=model_name,
            provider=provider_type,
            api_key=api_key,
            base_url=model_base_url,
            workspace_root=workspace_root,
            storage=self._thread_storage,
            instructions=self._compose_instructions(server_rules, include_image_tool=image_config is not None),
            system_prompt=self._system_prompt_extra or None,
            mcp_servers=mcp_servers,
            image_config=image_config,
            static_files_manager=self.static_files_manager,
            usage_limits=DEFAULT_AGENT_USAGE_LIMITS,
        )
        self._runner_cache[cache_key] = runner
        return runner

    def on_handle_list_agent_providers_request(self, _: ListAgentProvidersRequest) -> ResultPayload:
        return ListAgentProvidersResultSuccess(
            providers=list(self._providers),
            active_provider=self._active_provider_name,
            result_details="Chat providers retrieved successfully.",
        )

    def on_handle_create_agent_provider_request(self, request: CreateAgentProviderRequest) -> ResultPayload:
        name = str(request.provider.get("name", "")).strip()
        if not name:
            return CreateAgentProviderResultFailure(
                result_details="Attempted to create chat provider. Failed because 'name' is required."
            )
        provider_type = str(request.provider.get("type", ""))
        if provider_type not in _VALID_PROVIDER_TYPES:
            return CreateAgentProviderResultFailure(
                result_details=f"Attempted to create provider '{name}'. Failed because type '{provider_type}' is not a known preset id."
            )
        if any(p.get("name") == name for p in self._providers):
            return CreateAgentProviderResultFailure(
                result_details=f"Attempted to create provider. Failed because a provider named '{name}' already exists."
            )
        self._providers.append(dict(request.provider))
        self._persist_providers()
        self._runner_cache.clear()
        return CreateAgentProviderResultSuccess(name=name, result_details=f"Provider '{name}' created successfully.")

    def on_handle_update_agent_provider_request(self, request: UpdateAgentProviderRequest) -> ResultPayload:
        existing = next((p for p in self._providers if p.get("name") == request.name), None)
        if existing is None:
            return UpdateAgentProviderResultFailure(
                result_details=f"Attempted to update provider '{request.name}'. Failed because it does not exist."
            )
        if "type" in request.provider and str(request.provider["type"]) not in _VALID_PROVIDER_TYPES:
            return UpdateAgentProviderResultFailure(
                result_details=f"Attempted to update provider '{request.name}'. Failed because type '{request.provider['type']}' is not a known preset id."
            )
        existing.update(request.provider)
        existing["name"] = request.name  # name is the stable key — never allow rename via update
        self._persist_providers()
        self._runner_cache.clear()
        return UpdateAgentProviderResultSuccess(result_details=f"Provider '{request.name}' updated successfully.")

    def on_handle_delete_agent_provider_request(self, request: DeleteAgentProviderRequest) -> ResultPayload:
        if request.name == _PROTECTED_PROVIDER_NAME:
            return DeleteAgentProviderResultFailure(
                result_details=f"Attempted to delete provider '{request.name}'. Failed because it is a protected provider."
            )
        idx = next((i for i, p in enumerate(self._providers) if p.get("name") == request.name), None)
        if idx is None:
            return DeleteAgentProviderResultFailure(
                result_details=f"Attempted to delete provider '{request.name}'. Failed because it does not exist."
            )
        if len(self._providers) <= 1:
            return DeleteAgentProviderResultFailure(
                result_details=f"Attempted to delete provider '{request.name}'. Failed because it is the last remaining provider."
            )
        self._providers.pop(idx)
        if self._active_provider_name == request.name:
            self._active_provider_name = str(self._providers[0].get("name", _PROTECTED_PROVIDER_NAME))
        self._persist_providers()
        self._runner_cache.clear()
        return DeleteAgentProviderResultSuccess(
            name=request.name, result_details=f"Provider '{request.name}' deleted successfully."
        )

    def _load_providers_from_config(self) -> None:
        """Load providers list and active provider from config, with legacy migration.

        The griptape_cloud provider is always synthesized — it never needs to appear
        in the config file. Its model override lives in agent.griptape_cloud_model.
        """
        default_model = MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o"
        gc_model = str(config_manager.get_config_value("agent.griptape_cloud_model") or default_model)
        gc_provider: dict = {"name": _PROTECTED_PROVIDER_NAME, "type": _PROTECTED_PROVIDER_NAME, "model": gc_model}

        raw_providers = config_manager.get_config_value("agent.providers")
        if isinstance(raw_providers, list):
            # Strip any griptape_cloud entry — it is always synthesized above.
            user_providers = [p for p in raw_providers if p.get("name") != _PROTECTED_PROVIDER_NAME]
            self._providers = [gc_provider, *user_providers]
        else:
            self._providers = [gc_provider, *self._migrate_legacy_user_providers()]

        saved_active = config_manager.get_config_value("agent.active_provider")
        provider_names = {p.get("name") for p in self._providers}
        if isinstance(saved_active, str) and saved_active in provider_names:
            self._active_provider_name = saved_active
        else:
            self._active_provider_name = _PROTECTED_PROVIDER_NAME

    def _migrate_legacy_user_providers(self) -> list[dict]:
        """Return user-defined providers migrated from the old flat agent.provider config.

        Returns only non-griptape_cloud entries; gc is always synthesized separately.
        """
        legacy = config_manager.get_config_value("agent.provider") or {}
        if isinstance(legacy, str):
            legacy = {"id": legacy}
        if not isinstance(legacy, dict) or not legacy:
            return []
        type_id = str(legacy.get("id", _PROTECTED_PROVIDER_NAME))
        if type_id == _PROTECTED_PROVIDER_NAME:
            return []
        default_model = MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o"
        # agent.model was a sibling key some users set intuitively; fall back to it
        # before the catalog default so the migration preserves their intent.
        model = str(legacy.get("model") or config_manager.get_config_value("agent.model") or default_model)
        migrated: dict = {"name": type_id, "type": type_id, "model": model}
        if "base_url" in legacy:
            migrated["base_url"] = legacy["base_url"]
        if "api_key" in legacy:
            migrated["api_key"] = legacy["api_key"]
        return [migrated]

    def _persist_providers(self) -> None:
        """Write chat provider state to config.

        The griptape_cloud entry is never written to agent.providers — it is
        always synthesized on load. Its model override (when changed from default)
        is stored separately under agent.griptape_cloud_model.
        """
        user_providers = [p for p in self._providers if p.get("name") != _PROTECTED_PROVIDER_NAME]
        config_manager.set_config_value("agent.providers", user_providers)
        config_manager.set_config_value("agent.active_provider", self._active_provider_name)

        gc = next((p for p in self._providers if p.get("name") == _PROTECTED_PROVIDER_NAME), None)
        default_model = MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o"
        if gc and gc.get("model") != default_model:
            config_manager.set_config_value("agent.griptape_cloud_model", gc.get("model"))

    def _compose_instructions(self, server_rules: list[str], *, include_image_tool: bool) -> str:
        """Compose the instructions string from base rules and per-MCP-server rules."""
        parts = [_build_agent_instructions(include_image_tool=include_image_tool)]
        parts.extend(server_rules)
        return "\n\n".join(parts)

    @staticmethod
    def _lookup_mcp_configs(server_names: list[str]) -> list[dict[str, Any]]:
        if not server_names:
            return []
        result = GriptapeNodes.handle_request(GetEnabledMCPServersRequest())
        if not isinstance(result, GetEnabledMCPServersResultSuccess):
            logger.warning("Could not load enabled MCP servers; agent will run without extras.")
            return []
        return [{**result.servers[name], "name": name} for name in server_names if name in result.servers]

    def _validate_thread_for_run(self, thread_id: str | None) -> str:
        if thread_id is None or not self._thread_storage.thread_exists(thread_id):
            new_id, _ = self._thread_storage.create_thread()
            return new_id

        meta = self._thread_storage.get_thread_metadata(thread_id)
        if meta.get("archived", False):
            details = f"Cannot run agent on archived thread {thread_id}. Unarchive it first."
            raise ValueError(details)
        return thread_id


async def _compose_prompt(text: str, url_artifacts: list[RunAgentRequestArtifact]) -> str | list[UserContent]:
    """Combine the plain text input with any attached image artifacts.

    Image attachments are downloaded server-side and inlined as
    ``BinaryContent`` so the model receives the actual pixels rather than a URL.
    The engine fetches the bytes itself, which is why this works even when the
    static file store hands out localhost URLs the model provider cannot reach.

    Returns the plain ``text`` when there are no usable image attachments,
    otherwise a ``[text, BinaryContent, ...]`` sequence for ``Agent.run``.
    """
    image_urls = [
        artifact.value for artifact in url_artifacts if artifact.type == "ImageUrlArtifact" and artifact.value
    ]
    if not image_urls:
        return text

    contents: list[UserContent] = []
    if text:
        contents.append(text)
    async with httpx.AsyncClient(timeout=_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS) as client:
        for url in image_urls:
            content = await _download_image_content(client, url)
            if content is not None:
                contents.append(content)

    # Every download failed: fall back to plain text so the turn still runs.
    if not any(isinstance(content, BinaryContent) for content in contents):
        return text
    return contents


async def _download_image_content(client: httpx.AsyncClient, url: str) -> BinaryContent | None:
    """Download an image URL and wrap its bytes as inline ``BinaryContent``.

    Returns ``None`` when the download fails so the caller can drop the
    attachment and still run the turn with whatever else succeeded.
    """
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Attempted to attach image from %s. Skipping it because the download failed with %s.", url, e)
        return None
    return BinaryContent(data=response.content, media_type=_resolve_image_media_type(response, url))


def _resolve_image_media_type(response: httpx.Response, url: str) -> str:
    """Determine the image media type from the response header, then the URL.

    Prefers the server's ``Content-Type`` and falls back to guessing from the
    URL path (query string stripped) before defaulting to PNG.
    """
    header_media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if header_media_type.startswith("image/"):
        return header_media_type
    guessed_media_type, _ = mimetypes.guess_type(urlsplit(url).path)
    if guessed_media_type and guessed_media_type.startswith("image/"):
        return guessed_media_type
    return _DEFAULT_IMAGE_MEDIA_TYPE


def _run_event_to_payload(event: RunEvent) -> Any:
    """Translate a runner event into the matching ExecutionPayload.

    Returns ``None`` for event kinds that don't have a UI counterpart yet.
    """
    if isinstance(event, TextDelta):
        return AgentStreamEvent(token=event.delta)
    if isinstance(event, ToolCall):
        return AgentToolCallEvent(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            args=event.args,
        )
    if isinstance(event, ToolResult):
        return AgentToolResultEvent(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            content=event.content,
            is_error=event.is_error,
        )
    if isinstance(event, ThinkingDelta):
        return AgentThinkingEvent(delta=event.delta)
    return None
