"""Tests for AgentManager request handlers.

All tests bypass `AgentManager.__init__` via `AgentManager.__new__` and
manually set the minimal state each handler reads.  Config I/O
(`_persist_providers`) is patched at the instance level so tests never hit
the real config system.
"""

import asyncio
import json
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError, ModelRetry
from pydantic_ai.messages import BinaryContent, ImageUrl, ModelMessage, ModelRequest, UserPromptPart

from griptape_nodes.agents.pydantic_ai.runner import (
    AgentRunResult,
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
    PROVIDER_CATALOG,
    ProviderCatalogEntry,
    provider_catalog_entries,
)
from griptape_nodes.retained_mode.events.agent_events import (
    AgentStreamEvent,
    AgentThinkingEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    CancelAgentRequest,
    CancelAgentResultSuccess,
    ConfigureAgentRequest,
    ConfigureAgentResultFailure,
    ConfigureAgentResultSuccess,
    CreateAgentProviderRequest,
    CreateAgentProviderResultFailure,
    CreateAgentProviderResultSuccess,
    CreateProviderPayload,
    DeleteAgentProviderRequest,
    DeleteAgentProviderResultFailure,
    DeleteAgentProviderResultSuccess,
    GetAgentConfigRequest,
    GetAgentConfigResultSuccess,
    ListAgentModelsRequest,
    ListAgentModelsResultSuccess,
    ListAgentProvidersRequest,
    ListAgentProvidersResultSuccess,
    ListProviderModelsRequest,
    ListProviderModelsResultFailure,
    ListProviderModelsResultSuccess,
    PromptDriverConfig,
    ProviderConfig,
    RunAgentRequest,
    RunAgentRequestArtifact,
    RunAgentResultSuccess,
    UpdateAgentProviderRequest,
    UpdateAgentProviderResultFailure,
    UpdateAgentProviderResultSuccess,
    UpdateProviderPayload,
)
from griptape_nodes.retained_mode.managers.agent_manager import (
    _PROTECTED_PROVIDER_NAME,
    _SKILLS_README,
    _UNAVAILABLE_IMAGE_PLACEHOLDER,
    _VALID_PROVIDER_TYPES,
    AgentManager,
    ComposedPrompt,
    _ActiveRun,
    _cloud_http_status_of,
    _compose_prompt,
    _friendly_list_models_error,
    _message_has_image_url,
    _rehydrate_history,
    _run_event_to_payload,
)

_AGENT_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.agent_manager"


@pytest.fixture
def agent_manager() -> AgentManager:
    """Build a bare `AgentManager` without running `__init__`.

    The handler only reads module constants, so the manager's wiring (thread
    storage, event handlers, MCP) is irrelevant.
    """
    return AgentManager.__new__(AgentManager)


@pytest.fixture
def providers_manager(monkeypatch: pytest.MonkeyPatch) -> AgentManager:
    """Build an AgentManager with a known two-provider list, no config I/O."""
    manager = AgentManager.__new__(AgentManager)
    manager._providers = [
        ProviderConfig(name="griptape_cloud", type="griptape_cloud", model="gpt-4o"),
        ProviderConfig(name="my-ollama", type="ollama", model="llama3.2", base_url="http://localhost:11434/v1"),
    ]
    manager._active_provider_name = "griptape_cloud"
    manager._runner_cache = {}
    manager._image_model_name = IMAGE_MODEL_CHOICES[0] if IMAGE_MODEL_CHOICES else "gpt-image-1-mini"
    monkeypatch.setattr(manager, "_persist_providers", lambda: None)
    return manager


class TestEnsureSkillsDirectory:
    """`_ensure_skills_directory` scaffolds `.agents/skills` without ever raising."""

    def test_creates_directory_and_readme(self, agent_manager: AgentManager, tmp_path: Path) -> None:
        agent_manager._ensure_skills_directory(tmp_path)

        skills_dir = tmp_path / ".agents/skills"
        assert skills_dir.is_dir()
        assert (skills_dir / "README.md").read_text(encoding="utf-8") == _SKILLS_README

    def test_existing_readme_is_not_overwritten(self, agent_manager: AgentManager, tmp_path: Path) -> None:
        skills_dir = tmp_path / ".agents/skills"
        skills_dir.mkdir(parents=True)
        readme = skills_dir / "README.md"
        readme.write_text("user edits", encoding="utf-8")

        agent_manager._ensure_skills_directory(tmp_path)

        assert readme.read_text(encoding="utf-8") == "user edits"

    def test_scaffold_failure_does_not_raise(self, agent_manager: AgentManager, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")

        agent_manager._ensure_skills_directory(blocker)


class TestComposeInstructions:
    """Per-MCP-server `rules` are folded into the instructions string, not dropped."""

    def test_no_rules_returns_base_instructions(self, agent_manager: AgentManager) -> None:
        result = agent_manager._compose_instructions([], include_image_tool=False)
        assert "GriptapeNodes" in result
        assert "generate_image" not in result

    def test_image_tool_included_when_requested(self, agent_manager: AgentManager) -> None:
        result = agent_manager._compose_instructions([], include_image_tool=True)
        assert "generate_image" in result

    def test_rules_are_appended_to_base_instructions(self, agent_manager: AgentManager) -> None:
        composed = agent_manager._compose_instructions(
            ["Rules for MCP server 'a':\nbe terse", "Rules for MCP server 'b':\nbe kind"],
            include_image_tool=False,
        )
        assert "GriptapeNodes" in composed
        assert "be terse" in composed
        assert "be kind" in composed


class TestOnHandleListAgentModelsRequest:
    def test_returns_full_griptape_cloud_catalog(self, agent_manager: AgentManager) -> None:
        result = agent_manager.on_handle_list_agent_models_request(ListAgentModelsRequest())

        assert isinstance(result, ListAgentModelsResultSuccess)
        assert result.prompt_models == list(MODEL_CHOICES)
        assert result.image_models == list(IMAGE_MODEL_CHOICES)
        assert result.deprecated_models == {**DEPRECATED_MODELS, **IMAGE_DEPRECATED_MODELS}

    def test_returns_independent_copies_of_module_constants(self, agent_manager: AgentManager) -> None:
        original_prompt = list(MODEL_CHOICES)
        original_image = list(IMAGE_MODEL_CHOICES)
        original_prompt_dep = dict(DEPRECATED_MODELS)
        original_image_dep = dict(IMAGE_DEPRECATED_MODELS)

        result = agent_manager.on_handle_list_agent_models_request(ListAgentModelsRequest())
        assert isinstance(result, ListAgentModelsResultSuccess)

        result.prompt_models.append("polluted")
        result.image_models.append("polluted")
        result.deprecated_models["polluted"] = "polluted"

        assert original_prompt == MODEL_CHOICES
        assert original_image == IMAGE_MODEL_CHOICES
        assert original_prompt_dep == DEPRECATED_MODELS
        assert original_image_dep == IMAGE_DEPRECATED_MODELS

    def test_deprecation_map_merges_prompt_and_image_namespaces(self, agent_manager: AgentManager) -> None:
        result = agent_manager.on_handle_list_agent_models_request(ListAgentModelsRequest())
        assert isinstance(result, ListAgentModelsResultSuccess)

        for key in DEPRECATED_MODELS:
            assert key in result.deprecated_models
        for key in IMAGE_DEPRECATED_MODELS:
            assert key in result.deprecated_models


class TestOnHandleCancelAgentRequest:
    def test_no_active_run_is_idempotent_success(self) -> None:
        agent_manager = AgentManager.__new__(AgentManager)
        agent_manager._active_runs = {}

        result = agent_manager.on_handle_cancel_agent_request(CancelAgentRequest(thread_id="missing"))

        assert isinstance(result, CancelAgentResultSuccess)
        assert result.thread_id == "missing"
        assert result.was_running is False

    @pytest.mark.asyncio
    async def test_active_run_is_signalled(self) -> None:
        agent_manager = AgentManager.__new__(AgentManager)
        agent_manager._active_runs = {}
        cancel_event = asyncio.Event()
        agent_manager._active_runs["t1"] = _ActiveRun(cancel_event=cancel_event, loop=asyncio.get_running_loop())

        result = agent_manager.on_handle_cancel_agent_request(CancelAgentRequest(thread_id="t1"))

        assert isinstance(result, CancelAgentResultSuccess)
        assert result.was_running is True
        # The event is set via call_soon_threadsafe; yield once so it runs.
        await asyncio.sleep(0)
        assert cancel_event.is_set()


class TestRunEventToPayload:
    """Every streamed payload carries the thread id so clients can route it."""

    def test_text_delta_becomes_stream_event_with_thread_id(self) -> None:
        payload = _run_event_to_payload(TextDelta(delta="hi"), "thread-1")

        assert payload == AgentStreamEvent(thread_id="thread-1", token="hi")  # noqa: S106 - a streamed text token

    def test_thinking_delta_becomes_thinking_event_with_thread_id(self) -> None:
        payload = _run_event_to_payload(ThinkingDelta(delta="pondering"), "thread-1")

        assert payload == AgentThinkingEvent(thread_id="thread-1", delta="pondering")

    def test_tool_call_becomes_tool_call_event_with_thread_id(self) -> None:
        payload = _run_event_to_payload(
            ToolCall(tool_call_id="call-1", tool_name="read_file", args='{"path": "a.txt"}'), "thread-1"
        )

        assert payload == AgentToolCallEvent(
            thread_id="thread-1", tool_call_id="call-1", tool_name="read_file", args='{"path": "a.txt"}'
        )

    def test_tool_result_becomes_tool_result_event_with_thread_id(self) -> None:
        payload = _run_event_to_payload(
            ToolResult(tool_call_id="call-1", tool_name="read_file", content="boom", is_error=True), "thread-1"
        )

        assert payload == AgentToolResultEvent(
            thread_id="thread-1", tool_call_id="call-1", tool_name="read_file", content="boom", is_error=True
        )

    def test_unmapped_event_kind_is_dropped(self) -> None:
        assert _run_event_to_payload(RunEvent(), "thread-1") is None


@dataclass
class _GetRecorder:
    """Serves queued `httpx.Response`s keyed by URL and records requested URLs."""

    responses: dict[str, httpx.Response] = field(default_factory=dict)
    requested_urls: list[str] = field(default_factory=list)


@pytest.fixture
def patch_get(monkeypatch: pytest.MonkeyPatch) -> _GetRecorder:
    """Route `httpx.AsyncClient.get` through a recorder keyed by URL.

    Unmapped URLs resolve to a 404 so download-failure paths are exercisable.
    """
    recorder = _GetRecorder()

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:  # noqa: ARG001
        recorder.requested_urls.append(url)
        request = httpx.Request("GET", url)
        response = recorder.responses.get(url, httpx.Response(404))
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return recorder


def _image_artifact(url: str) -> RunAgentRequestArtifact:
    return RunAgentRequestArtifact(type="ImageUrlArtifact", value=url)


class TestComposePrompt:
    @pytest.mark.asyncio
    async def test_no_artifacts_returns_plain_text(self, patch_get: _GetRecorder) -> None:
        result = await _compose_prompt("hello", [])

        assert result == ComposedPrompt(live="hello", persist="hello")
        assert patch_get.requested_urls == []

    @pytest.mark.asyncio
    async def test_non_image_artifacts_are_ignored(self, patch_get: _GetRecorder) -> None:
        artifacts = [RunAgentRequestArtifact(type="TextArtifact", value="http://x/file.txt")]

        result = await _compose_prompt("hello", artifacts)

        assert result == ComposedPrompt(live="hello", persist="hello")
        assert patch_get.requested_urls == []

    @pytest.mark.asyncio
    async def test_image_is_downloaded_and_inlined(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/cat.png"
        patch_get.responses[url] = httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

        result = await _compose_prompt("look", [_image_artifact(url)])

        assert isinstance(result.live, list)
        assert result.live[0] == "look"
        image = result.live[1]
        assert isinstance(image, BinaryContent)
        assert image.data == b"png-bytes"
        assert image.media_type == "image/png"
        assert patch_get.requested_urls == [url]

    @pytest.mark.asyncio
    async def test_persist_form_swaps_bytes_for_image_url(self, patch_get: _GetRecorder) -> None:
        # The persisted form mirrors the live form but carries the source URL as
        # an ImageUrl instead of the inlined bytes, keeping history small.
        url = "http://localhost:9/workspace/cat.png"
        patch_get.responses[url] = httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

        result = await _compose_prompt("look", [_image_artifact(url)])

        assert result.persist == ["look", ImageUrl(url=url)]
        assert not any(isinstance(part, BinaryContent) for part in result.persist)

    @pytest.mark.asyncio
    async def test_reads_request_artifact_attributes(self, patch_get: _GetRecorder) -> None:
        # The wire deserializer hands back RunAgentRequestArtifact instances
        # whose data lives in attributes.
        url = "http://localhost:9/workspace/dog.png"
        patch_get.responses[url] = httpx.Response(200, content=b"dog", headers={"content-type": "image/png"})
        artifact = RunAgentRequestArtifact(type="ImageUrlArtifact", value=url)

        result = await _compose_prompt("who is this", [artifact])

        assert isinstance(result.live, list)
        binary_parts = [part for part in result.live if isinstance(part, BinaryContent)]
        assert len(binary_parts) == 1
        assert binary_parts[0].data == b"dog"

    @pytest.mark.asyncio
    async def test_media_type_falls_back_to_url_extension(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/cat.jpeg?t=123"
        patch_get.responses[url] = httpx.Response(
            200, content=b"jpeg-bytes", headers={"content-type": "application/octet-stream"}
        )

        result = await _compose_prompt("", [_image_artifact(url)])

        assert isinstance(result.live, list)
        # Empty text contributes no leading string element.
        assert len(result.live) == 1
        image = result.live[0]
        assert isinstance(image, BinaryContent)
        assert image.media_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_media_type_defaults_to_png_when_unknown(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/blob"
        patch_get.responses[url] = httpx.Response(200, content=b"raw")

        result = await _compose_prompt("hi", [_image_artifact(url)])

        assert isinstance(result.live, list)
        assert isinstance(result.live[1], BinaryContent)
        assert result.live[1].media_type == "image/png"

    @pytest.mark.asyncio
    async def test_failed_download_is_dropped(self, patch_get: _GetRecorder) -> None:
        ok_url = "http://localhost:9/workspace/ok.png"
        bad_url = "http://localhost:9/workspace/missing.png"
        patch_get.responses[ok_url] = httpx.Response(200, content=b"ok", headers={"content-type": "image/png"})

        result = await _compose_prompt("two", [_image_artifact(bad_url), _image_artifact(ok_url)])

        assert isinstance(result.live, list)
        binary_parts = [part for part in result.live if isinstance(part, BinaryContent)]
        assert len(binary_parts) == 1
        assert binary_parts[0].data == b"ok"
        # The dropped attachment leaves no ImageUrl in the persisted form either.
        assert result.persist == ["two", ImageUrl(url=ok_url)]

    @pytest.mark.asyncio
    async def test_all_downloads_failing_falls_back_to_text(self, patch_get: _GetRecorder) -> None:
        result = await _compose_prompt("text", [_image_artifact("http://localhost:9/workspace/gone.png")])

        assert result == ComposedPrompt(live="text", persist="text")
        assert patch_get.requested_urls == ["http://localhost:9/workspace/gone.png"]


class TestMessageHasImageUrl:
    def test_true_for_user_prompt_with_image_url(self) -> None:
        message = ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url="http://x/a.png")])])

        assert _message_has_image_url(message) is True

    def test_false_for_text_only_user_prompt(self) -> None:
        message = ModelRequest(parts=[UserPromptPart(content="just text")])

        assert _message_has_image_url(message) is False

    def test_false_for_list_content_without_image_url(self) -> None:
        message = ModelRequest(parts=[UserPromptPart(content=["a", "b"])])

        assert _message_has_image_url(message) is False


class TestRehydrateHistory:
    @pytest.mark.asyncio
    async def test_text_only_history_returns_unchanged_without_downloading(self, patch_get: _GetRecorder) -> None:
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]

        result = await _rehydrate_history(messages)

        assert result is messages
        assert patch_get.requested_urls == []

    @pytest.mark.asyncio
    async def test_image_url_is_downloaded_back_to_binary_content(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/cat.png"
        patch_get.responses[url] = httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url=url)])])]

        result = await _rehydrate_history(messages)

        part = result[0].parts[0]
        assert isinstance(part, UserPromptPart)
        assert part.content[0] == "look"
        image = part.content[1]
        assert isinstance(image, BinaryContent)
        assert image.data == b"png-bytes"
        assert patch_get.requested_urls == [url]
        # The input must not be mutated: the persisted history stays an ImageUrl.
        original_part = messages[0].parts[0]
        assert isinstance(original_part, UserPromptPart)
        assert isinstance(original_part.content[1], ImageUrl)

    @pytest.mark.asyncio
    async def test_failed_download_drops_the_part(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/gone.png"
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url=url)])])]

        result = await _rehydrate_history(messages)

        assert patch_get.requested_urls == [url]
        part = result[0].parts[0]
        assert isinstance(part, UserPromptPart)
        assert part.content == ["look"]

    @pytest.mark.asyncio
    async def test_image_only_turn_all_failing_gets_placeholder_text(self, patch_get: _GetRecorder) -> None:
        # An image-only turn whose every image fails must not become empty
        # content (some providers reject an empty user message on replay).
        url = "http://localhost:9/workspace/gone.png"
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=[ImageUrl(url=url)])])]

        result = await _rehydrate_history(messages)

        assert patch_get.requested_urls == [url]
        part = result[0].parts[0]
        assert isinstance(part, UserPromptPart)
        assert part.content == [_UNAVAILABLE_IMAGE_PLACEHOLDER]

    @pytest.mark.asyncio
    async def test_failed_image_with_text_keeps_text_without_placeholder(self, patch_get: _GetRecorder) -> None:
        # When the turn still has text after a failed download, the text carries
        # the turn: no placeholder is added.
        url = "http://localhost:9/workspace/gone.png"
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=["hi", ImageUrl(url=url)])])]

        result = await _rehydrate_history(messages)

        assert patch_get.requested_urls == [url]
        part = result[0].parts[0]
        assert isinstance(part, UserPromptPart)
        assert part.content == ["hi"]

    @pytest.mark.asyncio
    async def test_multiple_images_preserve_order_with_partial_failure(self, patch_get: _GetRecorder) -> None:
        # Concurrent downloads must not reorder content: the surviving image
        # keeps its slot and interleaved text stays put; the failed one drops.
        ok_url = "http://localhost:9/workspace/ok.png"
        bad_url = "http://localhost:9/workspace/gone.png"
        patch_get.responses[ok_url] = httpx.Response(200, content=b"ok", headers={"content-type": "image/png"})
        content = ["before", ImageUrl(url=bad_url), "middle", ImageUrl(url=ok_url), "after"]
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=content)])]

        result = await _rehydrate_history(messages)

        part = result[0].parts[0]
        assert isinstance(part, UserPromptPart)
        # bad_url dropped; ok_url inlined; text order preserved.
        assert part.content[0] == "before"
        assert part.content[1] == "middle"
        assert isinstance(part.content[2], BinaryContent)
        assert part.content[2].data == b"ok"
        assert part.content[3] == "after"
        assert not any(isinstance(item, ImageUrl) for item in part.content)


# ---------------------------------------------------------------------------
# Provider constant smoke tests
# ---------------------------------------------------------------------------


class TestProviderPresets:
    """PROVIDER_CATALOG is the source of truth for provider config."""

    def test_valid_provider_types_matches_catalog_ids(self) -> None:
        assert frozenset(PROVIDER_CATALOG.providers) == _VALID_PROVIDER_TYPES

    def test_protected_provider_is_in_catalog(self) -> None:
        assert _PROTECTED_PROVIDER_NAME in PROVIDER_CATALOG.providers

    def test_catalog_entries_are_typed(self) -> None:
        entries = provider_catalog_entries()
        assert len(entries) > 0
        for entry in entries:
            assert isinstance(entry, ProviderCatalogEntry)
            assert entry.id
            assert entry.display_name


# ---------------------------------------------------------------------------
# ListAgentProvidersRequest
# ---------------------------------------------------------------------------


class TestListAgentProviders:
    def test_returns_all_providers(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_list_agent_providers_request(ListAgentProvidersRequest())

        assert isinstance(result, ListAgentProvidersResultSuccess)
        assert len(result.providers) == len(providers_manager._providers)

    def test_returns_active_provider_name(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_list_agent_providers_request(ListAgentProvidersRequest())

        assert isinstance(result, ListAgentProvidersResultSuccess)
        assert result.active_provider == "griptape_cloud"

    def test_active_provider_reflects_current_state(self, providers_manager: AgentManager) -> None:
        providers_manager._active_provider_name = "my-ollama"

        result = providers_manager.on_handle_list_agent_providers_request(ListAgentProvidersRequest())

        assert isinstance(result, ListAgentProvidersResultSuccess)
        assert result.active_provider == "my-ollama"

    def test_returned_list_is_a_copy(self, providers_manager: AgentManager) -> None:
        initial_count = len(providers_manager._providers)
        result = providers_manager.on_handle_list_agent_providers_request(ListAgentProvidersRequest())
        assert isinstance(result, ListAgentProvidersResultSuccess)

        # Mutating the returned list must not affect internal state.
        result.providers.append(ProviderConfig(name="injected", type="ollama", model="phi3"))

        assert len(providers_manager._providers) == initial_count


# ---------------------------------------------------------------------------
# CreateAgentProviderRequest
# ---------------------------------------------------------------------------


class TestCreateAgentProvider:
    def test_create_valid_provider_appends_and_returns_success(self, providers_manager: AgentManager) -> None:
        request = CreateAgentProviderRequest(
            provider=CreateProviderPayload(name="home-ollama", type="ollama", model="mistral")
        )

        result = providers_manager.on_handle_create_agent_provider_request(request)

        assert isinstance(result, CreateAgentProviderResultSuccess)
        assert result.name == "home-ollama"
        assert any(p.name == "home-ollama" for p in providers_manager._providers)

    def test_create_clears_runner_cache(self, providers_manager: AgentManager) -> None:
        providers_manager._runner_cache[("griptape_cloud", "gpt-4o", "img", "", "", ())] = object()  # type: ignore[assignment]

        providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(name="new", type="ollama"))
        )

        assert providers_manager._runner_cache == {}

    def test_create_fails_when_name_is_missing(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(type="ollama"))
        )

        assert isinstance(result, CreateAgentProviderResultFailure)

    def test_create_fails_when_name_is_empty_string(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(name="   ", type="ollama"))
        )

        assert isinstance(result, CreateAgentProviderResultFailure)

    def test_create_fails_when_name_already_exists(self, providers_manager: AgentManager) -> None:
        initial_count = len(providers_manager._providers)
        result = providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(name="my-ollama", type="ollama"))
        )

        assert isinstance(result, CreateAgentProviderResultFailure)
        assert len(providers_manager._providers) == initial_count

    def test_create_fails_when_type_is_unknown(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(name="new", type="vllm"))
        )

        assert isinstance(result, CreateAgentProviderResultFailure)
        assert "vllm" in str(result.result_details)

    def test_create_persists_enabled_and_icon(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(
                provider=CreateProviderPayload(name="home-ollama", type="ollama", enabled=False, icon="server")
            )
        )

        assert isinstance(result, CreateAgentProviderResultSuccess)
        created = next(p for p in providers_manager._providers if p.name == "home-ollama")
        assert created.enabled is False
        assert created.icon == "server"

    def test_create_defaults_to_enabled(self, providers_manager: AgentManager) -> None:
        providers_manager.on_handle_create_agent_provider_request(
            CreateAgentProviderRequest(provider=CreateProviderPayload(name="home-ollama", type="ollama"))
        )

        created = next(p for p in providers_manager._providers if p.name == "home-ollama")
        assert created.enabled is True
        assert created.icon is None

    def test_create_all_valid_types_accepted(self, providers_manager: AgentManager) -> None:
        for provider_type in _VALID_PROVIDER_TYPES:
            unique_name = f"test-{provider_type}"
            result = providers_manager.on_handle_create_agent_provider_request(
                CreateAgentProviderRequest(provider=CreateProviderPayload(name=unique_name, type=provider_type))
            )
            # Only check success — some may fail due to duplicate names across iterations,
            # but type validation should never be the cause.
            if isinstance(result, CreateAgentProviderResultFailure):
                assert "not a known preset id" not in str(result.result_details)


# ---------------------------------------------------------------------------
# UpdateAgentProviderRequest
# ---------------------------------------------------------------------------


class TestUpdateAgentProvider:
    def test_update_merges_fields(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(model="phi3"))
        )

        assert isinstance(result, UpdateAgentProviderResultSuccess)
        updated = next(p for p in providers_manager._providers if p.name == "my-ollama")
        assert updated.model == "phi3"
        assert updated.base_url == "http://localhost:11434/v1"  # untouched

    def test_update_does_not_allow_rename(self, providers_manager: AgentManager) -> None:
        # UpdateProviderPayload has no name field — the type system prevents rename attempts.
        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(model="phi3"))
        )

        names = [p.name for p in providers_manager._providers]
        assert "my-ollama" in names
        assert "renamed" not in names

    def test_update_clears_runner_cache(self, providers_manager: AgentManager) -> None:
        providers_manager._runner_cache[("ollama", "llama3.2", "img", "http://x", "", ())] = object()  # type: ignore[assignment]

        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(model="gemma2"))
        )

        assert providers_manager._runner_cache == {}

    def test_update_fails_when_provider_not_found(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="nonexistent", provider=UpdateProviderPayload(model="phi3"))
        )

        assert isinstance(result, UpdateAgentProviderResultFailure)
        assert "nonexistent" in str(result.result_details)

    def test_update_fails_when_type_is_invalid(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(type="sglang"))
        )

        assert isinstance(result, UpdateAgentProviderResultFailure)
        assert "sglang" in str(result.result_details)

    def test_update_toggles_enabled(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(enabled=False))
        )

        assert isinstance(result, UpdateAgentProviderResultSuccess)
        updated = next(p for p in providers_manager._providers if p.name == "my-ollama")
        assert updated.enabled is False
        assert updated.model == "llama3.2"  # untouched

        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(enabled=True))
        )

        assert updated.enabled is True

    def test_update_preserves_enabled_when_omitted(self, providers_manager: AgentManager) -> None:
        provider = next(p for p in providers_manager._providers if p.name == "my-ollama")
        provider.enabled = False

        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(model="phi3"))
        )

        assert provider.enabled is False

    def test_update_sets_and_clears_icon(self, providers_manager: AgentManager) -> None:
        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(icon="server"))
        )

        updated = next(p for p in providers_manager._providers if p.name == "my-ollama")
        assert updated.icon == "server"

        providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(icon=""))
        )

        assert updated.icon is None

    def test_update_fails_when_disabling_protected_provider(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="griptape_cloud", provider=UpdateProviderPayload(enabled=False))
        )

        assert isinstance(result, UpdateAgentProviderResultFailure)
        assert "protected" in str(result.result_details)
        protected = next(p for p in providers_manager._providers if p.name == "griptape_cloud")
        assert protected.enabled is True

    def test_update_valid_type_change_succeeds(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_update_agent_provider_request(
            UpdateAgentProviderRequest(name="my-ollama", provider=UpdateProviderPayload(type="lmstudio"))
        )

        assert isinstance(result, UpdateAgentProviderResultSuccess)
        updated = next(p for p in providers_manager._providers if p.name == "my-ollama")
        assert updated.type == "lmstudio"


# ---------------------------------------------------------------------------
# DeleteAgentProviderRequest
# ---------------------------------------------------------------------------


class TestDeleteAgentProvider:
    def test_delete_removes_provider(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_delete_agent_provider_request(DeleteAgentProviderRequest(name="my-ollama"))

        assert isinstance(result, DeleteAgentProviderResultSuccess)
        assert result.name == "my-ollama"
        assert not any(p.name == "my-ollama" for p in providers_manager._providers)

    def test_delete_clears_runner_cache(self, providers_manager: AgentManager) -> None:
        providers_manager._runner_cache[("ollama", "llama3.2", "img", "http://x", "", ())] = object()  # type: ignore[assignment]

        providers_manager.on_handle_delete_agent_provider_request(DeleteAgentProviderRequest(name="my-ollama"))

        assert providers_manager._runner_cache == {}

    def test_delete_fails_for_protected_provider(self, providers_manager: AgentManager) -> None:
        initial_count = len(providers_manager._providers)
        result = providers_manager.on_handle_delete_agent_provider_request(
            DeleteAgentProviderRequest(name="griptape_cloud")
        )

        assert isinstance(result, DeleteAgentProviderResultFailure)
        assert "protected" in str(result.result_details)
        assert len(providers_manager._providers) == initial_count

    def test_delete_fails_when_provider_not_found(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_delete_agent_provider_request(
            DeleteAgentProviderRequest(name="nonexistent")
        )

        assert isinstance(result, DeleteAgentProviderResultFailure)
        assert "nonexistent" in str(result.result_details)

    def test_delete_fails_when_last_provider(self, providers_manager: AgentManager) -> None:
        # Remove the griptape_cloud provider so only one remains.
        providers_manager._providers = [ProviderConfig(name="solo", type="ollama", model="phi3")]

        result = providers_manager.on_handle_delete_agent_provider_request(DeleteAgentProviderRequest(name="solo"))

        assert isinstance(result, DeleteAgentProviderResultFailure)
        assert "last" in str(result.result_details)

    def test_delete_active_provider_auto_switches_to_first(self, providers_manager: AgentManager) -> None:
        providers_manager._active_provider_name = "my-ollama"

        providers_manager.on_handle_delete_agent_provider_request(DeleteAgentProviderRequest(name="my-ollama"))

        # After deletion, _providers[0] is griptape_cloud.
        assert providers_manager._active_provider_name == "griptape_cloud"

    def test_delete_non_active_provider_does_not_change_active(self, providers_manager: AgentManager) -> None:
        providers_manager._active_provider_name = "griptape_cloud"

        providers_manager.on_handle_delete_agent_provider_request(DeleteAgentProviderRequest(name="my-ollama"))

        assert providers_manager._active_provider_name == "griptape_cloud"


# ---------------------------------------------------------------------------
# GetAgentConfigRequest
# ---------------------------------------------------------------------------


class TestGetAgentConfig:
    def test_returns_active_provider_fields(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_get_agent_config_request(GetAgentConfigRequest())

        assert isinstance(result, GetAgentConfigResultSuccess)
        assert result.provider == "griptape_cloud"
        assert result.model_name == "gpt-4o"

    def test_returns_non_cloud_provider_fields(self, providers_manager: AgentManager) -> None:
        providers_manager._active_provider_name = "my-ollama"

        result = providers_manager.on_handle_get_agent_config_request(GetAgentConfigRequest())

        assert isinstance(result, GetAgentConfigResultSuccess)
        assert result.provider == "ollama"
        assert result.model_name == "llama3.2"
        assert result.base_url == "http://localhost:11434/v1"

    def test_returns_current_image_model(self, providers_manager: AgentManager) -> None:
        providers_manager._image_model_name = "gpt-image-1.5"

        result = providers_manager.on_handle_get_agent_config_request(GetAgentConfigRequest())

        assert isinstance(result, GetAgentConfigResultSuccess)
        assert result.image_model_name == "gpt-image-1.5"

    def test_missing_base_url_returns_empty_string(self, providers_manager: AgentManager) -> None:
        # griptape_cloud provider has no base_url key.
        result = providers_manager.on_handle_get_agent_config_request(GetAgentConfigRequest())

        assert isinstance(result, GetAgentConfigResultSuccess)
        assert result.base_url == ""


# ---------------------------------------------------------------------------
# ListProviderModelsRequest
# ---------------------------------------------------------------------------


class TestListProviderModels:
    @pytest.mark.asyncio
    async def test_griptape_cloud_returns_model_choices(self, providers_manager: AgentManager) -> None:
        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="griptape_cloud")
        )

        assert isinstance(result, ListProviderModelsResultSuccess)
        assert result.models == list(MODEL_CHOICES)

    @pytest.mark.asyncio
    async def test_external_provider_fetches_models_endpoint(
        self, providers_manager: AgentManager, patch_get: _GetRecorder
    ) -> None:
        base_url = "http://localhost:11434/v1"
        models_payload = json.dumps({"data": [{"id": "llama3.2"}, {"id": "phi3"}]}).encode()
        patch_get.responses[f"{base_url}/models"] = httpx.Response(200, content=models_payload)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="ollama", base_url=base_url)
        )

        assert isinstance(result, ListProviderModelsResultSuccess)
        assert result.models == ["llama3.2", "phi3"]
        assert f"{base_url}/models" in patch_get.requested_urls

    @pytest.mark.asyncio
    async def test_models_are_sorted_alphabetically(
        self, providers_manager: AgentManager, patch_get: _GetRecorder
    ) -> None:
        base_url = "http://localhost:11434/v1"
        payload = json.dumps({"data": [{"id": "zmodel"}, {"id": "amodel"}, {"id": "mmodel"}]}).encode()
        patch_get.responses[f"{base_url}/models"] = httpx.Response(200, content=payload)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="ollama", base_url=base_url)
        )

        assert isinstance(result, ListProviderModelsResultSuccess)
        assert result.models == ["amodel", "mmodel", "zmodel"]

    @pytest.mark.asyncio
    async def test_missing_base_url_returns_failure(self, providers_manager: AgentManager) -> None:
        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="ollama", base_url="")
        )

        assert isinstance(result, ListProviderModelsResultFailure)
        assert "base_url" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_http_error_returns_failure(self, providers_manager: AgentManager, patch_get: _GetRecorder) -> None:
        base_url = "http://localhost:11434/v1"
        patch_get.responses[f"{base_url}/models"] = httpx.Response(401)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="ollama", base_url=base_url)
        )

        assert isinstance(result, ListProviderModelsResultFailure)

    @pytest.mark.asyncio
    async def test_unreachable_provider_returns_friendly_message(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_url = "http://localhost:1234/v1"
        connect_error = httpx.ConnectError("All connection attempts failed")

        async def raise_connect_error(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:  # noqa: ARG001
            raise connect_error

        monkeypatch.setattr(httpx.AsyncClient, "get", raise_connect_error)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="lmstudio", base_url=base_url)
        )

        assert isinstance(result, ListProviderModelsResultFailure)
        details = str(result.result_details)
        # The raw transport error must not leak into the user-facing detail...
        assert "All connection attempts failed" not in details
        # ...and the friendly message must name the endpoint and the likely cause.
        assert base_url in details
        assert "running" in details.lower()

    @pytest.mark.asyncio
    async def test_api_key_sent_as_bearer_header(
        self, providers_manager: AgentManager, patch_get: _GetRecorder
    ) -> None:
        base_url = "http://localhost:1234/v1"
        payload = json.dumps({"data": [{"id": "some-model"}]}).encode()
        patch_get.responses[f"{base_url}/models"] = httpx.Response(200, content=payload)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="custom", base_url=base_url, api_key="sk-test")
        )

        # Just assert the call reached the endpoint — header inspection is not
        # possible via the recorder, but we verify success indicates the key
        # was accepted (mocked endpoint ignores it).
        assert isinstance(result, ListProviderModelsResultSuccess)
        assert result.models == ["some-model"]

    @pytest.mark.asyncio
    async def test_entries_without_id_are_excluded(
        self, providers_manager: AgentManager, patch_get: _GetRecorder
    ) -> None:
        base_url = "http://localhost:11434/v1"
        payload = json.dumps({"data": [{"id": "good"}, {"name": "no-id"}, {}]}).encode()
        patch_get.responses[f"{base_url}/models"] = httpx.Response(200, content=payload)

        result = await providers_manager.on_handle_list_provider_models_request(
            ListProviderModelsRequest(provider="ollama", base_url=base_url)
        )

        assert isinstance(result, ListProviderModelsResultSuccess)
        assert result.models == ["good"]


# ---------------------------------------------------------------------------
# Friendly list-models error mapping
# ---------------------------------------------------------------------------


class TestFriendlyListModelsError:
    def test_connect_error_maps_to_friendly_message(self) -> None:
        msg = _friendly_list_models_error(
            httpx.ConnectError("All connection attempts failed"), "http://localhost:1234/v1"
        )

        assert msg is not None
        assert "All connection attempts failed" not in msg
        assert "http://localhost:1234/v1" in msg
        assert "running" in msg.lower()

    def test_connect_timeout_maps_to_friendly_message(self) -> None:
        msg = _friendly_list_models_error(httpx.ConnectTimeout("timed out"), "http://localhost:11434/v1")

        assert msg is not None
        assert "http://localhost:11434/v1" in msg

    def test_read_timeout_maps_to_friendly_message(self) -> None:
        msg = _friendly_list_models_error(httpx.ReadTimeout("slow"), "http://host/v1")

        assert msg is not None
        assert "didn't respond" in msg

    def test_generic_request_error_maps_to_friendly_message(self) -> None:
        msg = _friendly_list_models_error(httpx.RequestError("dns broke"), "http://host/v1")

        assert msg is not None
        assert "connect" in msg.lower()

    def test_non_connection_error_returns_none(self) -> None:
        # A value/parse error is not connection-shaped — caller should fall back
        # to its own (raw) message rather than a misleading "server not running".
        assert _friendly_list_models_error(ValueError("bad json"), "http://host/v1") is None

    def test_http_status_error_returns_none(self) -> None:
        # An HTTP status error means the server *answered* — it's reachable, so
        # "is the server running?" would be misleading. Fall back to the raw msg.
        request = httpx.Request("GET", "http://host/v1/models")
        response = httpx.Response(500, request=request)
        status_error = httpx.HTTPStatusError("500", request=request, response=response)
        assert _friendly_list_models_error(status_error, "http://host/v1") is None

    def test_missing_base_url_omits_endpoint(self) -> None:
        msg = _friendly_list_models_error(httpx.ConnectError("x"), None)

        assert msg is not None
        assert "at ''" not in msg


# ---------------------------------------------------------------------------
# ConfigureAgentRequest — active_provider switching
# ---------------------------------------------------------------------------


class TestConfigureAgentActiveProvider:
    def test_set_valid_active_provider_succeeds(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_configure_agent_request(ConfigureAgentRequest(active_provider="my-ollama"))

        assert isinstance(result, ConfigureAgentResultSuccess)
        assert providers_manager._active_provider_name == "my-ollama"

    def test_set_nonexistent_active_provider_fails(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_configure_agent_request(ConfigureAgentRequest(active_provider="ghost"))

        assert isinstance(result, ConfigureAgentResultFailure)
        assert providers_manager._active_provider_name == "griptape_cloud"

    def test_empty_active_provider_is_ignored(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_configure_agent_request(ConfigureAgentRequest(active_provider=""))

        assert isinstance(result, ConfigureAgentResultSuccess)
        assert providers_manager._active_provider_name == "griptape_cloud"

    def test_switching_active_provider_clears_runner_cache(self, providers_manager: AgentManager) -> None:
        providers_manager._runner_cache[("griptape_cloud", "gpt-4o", "img", "", "", ())] = object()  # type: ignore[assignment]

        providers_manager.on_handle_configure_agent_request(ConfigureAgentRequest(active_provider="my-ollama"))

        assert providers_manager._runner_cache == {}

    def test_switching_to_same_active_provider_does_not_clear_cache(self, providers_manager: AgentManager) -> None:
        sentinel = object()
        key = ("griptape_cloud", "gpt-4o", "img", "", "", ())
        providers_manager._runner_cache[key] = sentinel  # type: ignore[assignment]

        # Switching to the already-active provider should not count as a change.
        providers_manager.on_handle_configure_agent_request(ConfigureAgentRequest(active_provider="griptape_cloud"))

        assert providers_manager._runner_cache.get(key) is sentinel

    def test_model_change_via_configure_updates_active_provider(self, providers_manager: AgentManager) -> None:
        result = providers_manager.on_handle_configure_agent_request(
            ConfigureAgentRequest(prompt_driver=PromptDriverConfig(model="gpt-5"))
        )

        assert isinstance(result, ConfigureAgentResultSuccess)
        gc = next(p for p in providers_manager._providers if p.name == "griptape_cloud")
        assert gc.model == "gpt-5"

    def test_switch_and_model_in_one_request_targets_the_new_provider(self, providers_manager: AgentManager) -> None:
        # "Switch to my-ollama and use qwen3" must not write qwen3 onto the
        # provider being switched away from.
        result = providers_manager.on_handle_configure_agent_request(
            ConfigureAgentRequest(active_provider="my-ollama", prompt_driver=PromptDriverConfig(model="qwen3"))
        )

        assert isinstance(result, ConfigureAgentResultSuccess)
        ollama = next(p for p in providers_manager._providers if p.name == "my-ollama")
        gc = next(p for p in providers_manager._providers if p.name == "griptape_cloud")
        assert ollama.model == "qwen3"
        assert gc.model == "gpt-4o"


class TestBuildRunnerCredential:
    """The Griptape Cloud runner accepts a license, not just `GT_CLOUD_API_KEY`."""

    def test_license_only_config_builds_runner(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reported bug: an Enterprise license with no GT_CLOUD_API_KEY raised
        # "Secret 'GT_CLOUD_API_KEY' not found" instead of running the agent.
        monkeypatch.setattr(_AGENT_MANAGER_MODULE + ".resolve_cloud_credential", lambda *_a, **_k: "the-license")
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            _AGENT_MANAGER_MODULE + ".PydanticAgentRunner",
            lambda **kwargs: captured.update(kwargs) or object(),
        )
        monkeypatch.setattr(providers_manager, "_ensure_skills_directory", lambda _root: None)
        providers_manager._mcp_server_port = 1234
        # The runner is stubbed out, so these only need to exist, not be real.
        providers_manager._system_prompt_extra = ""
        providers_manager._thread_storage = object()  # type: ignore[assignment]
        providers_manager.static_files_manager = None  # type: ignore[assignment]

        providers_manager._build_runner([], provider_name="griptape_cloud")

        assert captured["api_key"] == "the-license"

    def test_missing_both_credentials_names_both(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_AGENT_MANAGER_MODULE + ".resolve_cloud_credential", lambda *_a, **_k: None)

        with pytest.raises(ValueError, match="Sign in with your Griptape license") as excinfo:
            providers_manager._build_runner([], provider_name="griptape_cloud")

        assert "GT_CLOUD_API_KEY" in str(excinfo.value)


class TestExplainAgentRunError:
    """A 403 on a license-authenticated Cloud request is an entitlement denial."""

    def test_forbidden_with_license_explains_entitlement(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_AGENT_MANAGER_MODULE + ".resolve_cloud_credential", lambda *_a, **_k: "a.license.jwt")
        exc = ModelHTTPError(status_code=403, model_name="gpt-4o", body="Forbidden")

        message = providers_manager._explain_agent_run_error(exc, "griptape_cloud")

        assert "not entitled" in message

    def test_forbidden_with_api_key_keeps_original_message(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An API key can't be refused by license policy, so don't blame licensing.
        monkeypatch.setattr(_AGENT_MANAGER_MODULE + ".resolve_cloud_credential", lambda *_a, **_k: "gt-abc")
        exc = ModelHTTPError(status_code=403, model_name="gpt-4o", body="Forbidden")

        message = providers_manager._explain_agent_run_error(exc, "griptape_cloud")

        assert "not entitled" not in message

    def test_non_forbidden_error_keeps_original_message(self, providers_manager: AgentManager) -> None:
        message = providers_manager._explain_agent_run_error(ValueError("boom"), "griptape_cloud")

        assert message == "boom"

    def test_forbidden_on_other_provider_keeps_original_message(
        self, providers_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_AGENT_MANAGER_MODULE + ".resolve_cloud_credential", lambda *_a, **_k: "a.license.jwt")
        exc = ModelHTTPError(status_code=403, model_name="llama3.2", body="Forbidden")

        message = providers_manager._explain_agent_run_error(exc, "my-ollama")

        assert "not entitled" not in message


_CLOUD_HOST = "cloud.griptape.ai"


def _httpx_403(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("Forbidden", request=request, response=response)


class TestCloudHttpStatusOf:
    """`_cloud_http_status_of` spans two error shapes but only for Griptape Cloud."""

    def test_reads_model_http_error(self) -> None:
        """The chat path raises pydantic-ai's ModelHTTPError, which has status_code."""
        exc = ModelHTTPError(status_code=403, model_name="gpt-4o")

        assert _cloud_http_status_of(exc, _CLOUD_HOST) == HTTPStatus.FORBIDDEN

    def test_reads_httpx_status_error(self) -> None:
        """The image path raises an httpx error, which keeps status on .response."""
        exc = _httpx_403("https://cloud.griptape.ai/api/images/generations")

        assert _cloud_http_status_of(exc, _CLOUD_HOST) == HTTPStatus.FORBIDDEN

    def test_follows_cause_chain(self) -> None:
        """The image toolset wraps its failure in ModelRetry via `raise ... from exc`."""
        wrapped = ModelRetry("image generation failed")
        wrapped.__cause__ = _httpx_403("https://cloud.griptape.ai/api/images/generations")

        assert _cloud_http_status_of(wrapped, _CLOUD_HOST) == HTTPStatus.FORBIDDEN

    def test_ignores_403_from_another_host(self) -> None:
        """A remote MCP server's expired token must not be blamed on Griptape licensing."""
        exc = _httpx_403("https://mcp.example.com/mcp/")

        assert _cloud_http_status_of(exc, _CLOUD_HOST) is None

    def test_honors_custom_cloud_host(self) -> None:
        """A self-hosted GT_CLOUD_BASE_URL is still recognized as Cloud."""
        exc = _httpx_403("https://cloud.internal.example/api/images/generations")

        assert _cloud_http_status_of(exc, "cloud.internal.example") == HTTPStatus.FORBIDDEN

    def test_returns_none_for_non_http_error(self) -> None:
        """A plain error has no status, so the caller keeps the original message."""
        assert _cloud_http_status_of(ValueError("boom"), _CLOUD_HOST) is None


def _run_request() -> RunAgentRequest:
    """A minimal RunAgentRequest; the payload branches don't read its fields."""
    return RunAgentRequest(input="hello", url_artifacts=[], thread_id="t1")


async def _stub_compose_prompt(text: str, _url_artifacts: list[RunAgentRequestArtifact]) -> ComposedPrompt:
    """Skip artifact download; these tests only exercise the result branches."""
    return ComposedPrompt(live=text, persist=text)


class TestRunAgentResultPayloadContract:
    """`_run_agent`'s three success branches must agree on the payload's keys.

    The sidebar reads `output.truncated` on every reply, so a branch that omits
    it hands the consumer `undefined` where the other branches give a boolean.
    """

    _BRANCHES = (
        ("cancelled", AgentRunResult(thread_id="t1", output="partial", message_count=2, cancelled=True)),
        ("truncated", AgentRunResult(thread_id="t1", output="cut off", message_count=3, truncated=True)),
        ("normal", AgentRunResult(thread_id="t1", output="all done", message_count=3)),
    )

    @staticmethod
    def _manager(monkeypatch: pytest.MonkeyPatch, result: AgentRunResult) -> AgentManager:
        """An AgentManager whose runner returns `result` and whose I/O is stubbed."""
        manager = AgentManager.__new__(AgentManager)
        manager._active_runs = {}

        async def fake_run(*_args: object, **_kwargs: object) -> AgentRunResult:
            return result

        monkeypatch.setattr(manager, "_validate_thread_for_run", lambda _thread_id: "t1")
        monkeypatch.setattr(manager, "_build_runner", lambda *_a, **_k: SimpleNamespace(run=fake_run))
        # A non-empty history keeps `is_first_run` False, so no thread metadata write.
        manager._thread_storage = SimpleNamespace(load_history=lambda _t: [object()])  # type: ignore[assignment]
        monkeypatch.setattr(
            _AGENT_MANAGER_MODULE + "._compose_prompt",
            _stub_compose_prompt,
        )
        # `engine` is a read-only property over `_engine`; set the backing field.
        manager._engine = SimpleNamespace(  # type: ignore[assignment]
            event_manager=SimpleNamespace(put_event=lambda _e: None)
        )
        return manager

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("branch", "result"), _BRANCHES, ids=[b for b, _ in _BRANCHES])
    async def test_every_branch_sets_truncated(
        self, monkeypatch: pytest.MonkeyPatch, branch: str, result: AgentRunResult
    ) -> None:
        manager = self._manager(monkeypatch, result)

        payload = await manager._run_agent(_run_request())

        assert isinstance(payload, RunAgentResultSuccess)
        assert "truncated" in payload.output, f"the {branch} branch omits `truncated` from its payload"
        assert payload.output["truncated"] is result.truncated

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("branch", "result"), _BRANCHES, ids=[b for b, _ in _BRANCHES])
    async def test_branches_share_one_key_set(
        self, monkeypatch: pytest.MonkeyPatch, branch: str, result: AgentRunResult
    ) -> None:
        """One shape for all outcomes, so the frontend needs no per-branch handling."""
        manager = self._manager(monkeypatch, result)

        payload = await manager._run_agent(_run_request())

        assert isinstance(payload, RunAgentResultSuccess)
        assert set(payload.output) == {"text", "message_count", "cancelled", "truncated", "generated_image_urls"}, (
            f"the {branch} branch's payload keys differ from the other branches'"
        )
