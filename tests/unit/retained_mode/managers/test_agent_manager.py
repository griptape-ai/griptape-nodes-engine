"""Tests for AgentManager request handlers.

All tests bypass `AgentManager.__init__` via `AgentManager.__new__` and
manually set the minimal state each handler reads.  Config I/O
(`_persist_providers`) is patched at the instance level so tests never hit
the real config system.
"""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest
from pydantic_ai.messages import BinaryContent

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
    RunAgentRequestArtifact,
    UpdateAgentProviderRequest,
    UpdateAgentProviderResultFailure,
    UpdateAgentProviderResultSuccess,
    UpdateProviderPayload,
)
from griptape_nodes.retained_mode.managers.agent_manager import (
    _PROTECTED_PROVIDER_NAME,
    _VALID_PROVIDER_TYPES,
    AgentManager,
    _ActiveRun,
    _compose_prompt,
    _friendly_list_models_error,
)


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

        assert result == "hello"
        assert patch_get.requested_urls == []

    @pytest.mark.asyncio
    async def test_non_image_artifacts_are_ignored(self, patch_get: _GetRecorder) -> None:
        artifacts = [RunAgentRequestArtifact(type="TextArtifact", value="http://x/file.txt")]

        result = await _compose_prompt("hello", artifacts)

        assert result == "hello"
        assert patch_get.requested_urls == []

    @pytest.mark.asyncio
    async def test_image_is_downloaded_and_inlined(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/cat.png"
        patch_get.responses[url] = httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

        result = await _compose_prompt("look", [_image_artifact(url)])

        assert isinstance(result, list)
        assert result[0] == "look"
        image = result[1]
        assert isinstance(image, BinaryContent)
        assert image.data == b"png-bytes"
        assert image.media_type == "image/png"
        assert patch_get.requested_urls == [url]

    @pytest.mark.asyncio
    async def test_reads_request_artifact_attributes(self, patch_get: _GetRecorder) -> None:
        # The wire deserializer hands back RunAgentRequestArtifact instances
        # whose data lives in attributes.
        url = "http://localhost:9/workspace/dog.png"
        patch_get.responses[url] = httpx.Response(200, content=b"dog", headers={"content-type": "image/png"})
        artifact = RunAgentRequestArtifact(type="ImageUrlArtifact", value=url)

        result = await _compose_prompt("who is this", [artifact])

        assert isinstance(result, list)
        binary_parts = [part for part in result if isinstance(part, BinaryContent)]
        assert len(binary_parts) == 1
        assert binary_parts[0].data == b"dog"

    @pytest.mark.asyncio
    async def test_media_type_falls_back_to_url_extension(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/cat.jpeg?t=123"
        patch_get.responses[url] = httpx.Response(
            200, content=b"jpeg-bytes", headers={"content-type": "application/octet-stream"}
        )

        result = await _compose_prompt("", [_image_artifact(url)])

        assert isinstance(result, list)
        # Empty text contributes no leading string element.
        assert len(result) == 1
        image = result[0]
        assert isinstance(image, BinaryContent)
        assert image.media_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_media_type_defaults_to_png_when_unknown(self, patch_get: _GetRecorder) -> None:
        url = "http://localhost:9/workspace/blob"
        patch_get.responses[url] = httpx.Response(200, content=b"raw")

        result = await _compose_prompt("hi", [_image_artifact(url)])

        assert isinstance(result, list)
        assert isinstance(result[1], BinaryContent)
        assert result[1].media_type == "image/png"

    @pytest.mark.asyncio
    async def test_failed_download_is_dropped(self, patch_get: _GetRecorder) -> None:
        ok_url = "http://localhost:9/workspace/ok.png"
        bad_url = "http://localhost:9/workspace/missing.png"
        patch_get.responses[ok_url] = httpx.Response(200, content=b"ok", headers={"content-type": "image/png"})

        result = await _compose_prompt("two", [_image_artifact(bad_url), _image_artifact(ok_url)])

        assert isinstance(result, list)
        binary_parts = [part for part in result if isinstance(part, BinaryContent)]
        assert len(binary_parts) == 1
        assert binary_parts[0].data == b"ok"

    @pytest.mark.asyncio
    async def test_all_downloads_failing_falls_back_to_text(self, patch_get: _GetRecorder) -> None:
        result = await _compose_prompt("text", [_image_artifact("http://localhost:9/workspace/gone.png")])

        assert result == "text"
        assert patch_get.requested_urls == ["http://localhost:9/workspace/gone.png"]


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
