"""Tests for `AgentManager.on_handle_list_agent_models_request`.

The handler is a thin wrapper over the module-level catalog constants in
`cloud_models.py`, so tests instantiate the manager without running its
`__init__` and exercise the handler directly.
"""

import asyncio
from dataclasses import dataclass, field

import httpx
import pytest
from pydantic_ai.messages import BinaryContent

from griptape_nodes.drivers.cloud_models import (
    DEPRECATED_MODELS,
    IMAGE_DEPRECATED_MODELS,
    IMAGE_MODEL_CHOICES,
    MODEL_CHOICES,
)
from griptape_nodes.retained_mode.events.agent_events import (
    CancelAgentRequest,
    CancelAgentResultSuccess,
    ListAgentModelsRequest,
    ListAgentModelsResultSuccess,
    RunAgentRequestArtifact,
)
from griptape_nodes.retained_mode.managers.agent_manager import AgentManager, _ActiveRun, _compose_prompt


@pytest.fixture
def agent_manager() -> AgentManager:
    """Build a bare `AgentManager` without running `__init__`.

    The handler only reads module constants, so the manager's wiring (thread
    storage, event handlers, MCP) is irrelevant.
    """
    return AgentManager.__new__(AgentManager)


class TestComposeInstructions:
    """Per-MCP-server `rules` are folded into the instructions string, not dropped."""

    def test_no_rules_returns_base_instructions(self, agent_manager: AgentManager) -> None:
        agent_manager._instructions = "BASE"
        assert agent_manager._compose_instructions([]) == "BASE"

    def test_rules_are_appended_to_base_instructions(self, agent_manager: AgentManager) -> None:
        agent_manager._instructions = "BASE"
        composed = agent_manager._compose_instructions(
            ["Rules for MCP server 'a':\nbe terse", "Rules for MCP server 'b':\nbe kind"]
        )
        assert composed.startswith("BASE")
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
