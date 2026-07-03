"""Unit tests for the Griptape Cloud image-generation toolset."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from pydantic_ai.exceptions import ModelRetry

from griptape_nodes.agents.pydantic_ai.image_tools import (
    ImageGenerationToolset,
    ImageGenerationToolsetConfig,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager


class _FakeStaticFilesManager:
    """Records the bytes/filename passed to `save_static_file` and returns a URL."""

    def __init__(self) -> None:
        self.saved: list[tuple[bytes, str]] = []

    def save_static_file(self, data: bytes, file_name: str) -> str:
        self.saved.append((data, file_name))
        return f"https://files.local/{file_name}"


@pytest.fixture
def static_files() -> _FakeStaticFilesManager:
    """A static file manager stub that records saves and returns a stable URL."""
    return _FakeStaticFilesManager()


def _make_toolset(
    config: ImageGenerationToolsetConfig, static_files: _FakeStaticFilesManager
) -> ImageGenerationToolset:
    """Build a toolset, casting the fake static file manager to the real type."""
    return ImageGenerationToolset(config, cast("StaticFilesManager", static_files))


def _image_artifact_response(image_bytes: bytes, image_format: str = "png") -> dict[str, Any]:
    return {
        "artifact": {
            "type": "ImageArtifact",
            "value": base64.b64encode(image_bytes).decode("ascii"),
            "format": image_format,
        }
    }


@dataclass
class _TransportRecorder:
    """Captures outgoing requests and supplies queued responses."""

    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch) -> _TransportRecorder:
    """Route `httpx.AsyncClient.post` through a recording mock transport.

    Returns a recorder so a test can assert on captured requests and enqueue
    custom responses. The mock returns a PNG artifact unless a response is
    queued on the recorder.
    """
    recorder = _TransportRecorder()

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ARG001
        request = httpx.Request("POST", url, json=kwargs.get("json"), headers=kwargs.get("headers"))
        recorder.requests.append(request)
        if recorder.responses:
            response = recorder.responses.pop(0)
        else:
            response = httpx.Response(200, json=_image_artifact_response(b"image-bytes"))
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return recorder


class TestConfig:
    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            ImageGenerationToolsetConfig(api_key="")

    def test_rejects_unknown_image_size(self) -> None:
        with pytest.raises(ValueError, match="Image size"):
            ImageGenerationToolsetConfig(api_key="k", image_size="999x999")

    def test_accepts_allowed_image_size(self) -> None:
        config = ImageGenerationToolsetConfig(api_key="k", image_size="1024x1024")
        assert config.image_size == "1024x1024"


@pytest.mark.asyncio
class TestGenerateImage:
    async def test_rejects_empty_prompt(self, static_files: _FakeStaticFilesManager) -> None:
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)
        with pytest.raises(ModelRetry, match="non-empty"):
            await toolset.generate_image("   ")

    async def test_saves_image_and_returns_url(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        url = await toolset.generate_image("a red bird")

        assert len(static_files.saved) == 1
        saved_bytes, filename = static_files.saved[0]
        assert saved_bytes == b"image-bytes"
        assert filename.endswith(".png")
        assert url == f"https://files.local/{filename}"
        # The request carried the prompt and the model in the driver config.
        body = patch_transport.requests[0].read().decode()
        assert "a red bird" in body
        assert "gpt-image-1-mini" in body

    async def test_includes_negative_prompt_when_set(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        await toolset.generate_image("a red bird", negative_prompt="blurry")

        body = patch_transport.requests[0].read().decode()
        assert "negative_prompts" in body
        assert "blurry" in body

    async def test_omits_negative_prompt_when_blank(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        await toolset.generate_image("a red bird", negative_prompt="   ")

        body = patch_transport.requests[0].read().decode()
        assert "negative_prompts" not in body

    async def test_only_sends_set_driver_options(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        toolset = _make_toolset(
            ImageGenerationToolsetConfig(api_key="k", image_size="1536x1024", quality="high"),
            static_files,
        )

        await toolset.generate_image("a cat")

        body = patch_transport.requests[0].read().decode()
        assert "1536x1024" in body
        assert "high" in body
        # Unset options are not sent.
        assert "background" not in body
        assert "output_format" not in body

    async def test_uses_artifact_format_for_extension(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        patch_transport.responses.append(
            httpx.Response(200, json=_image_artifact_response(b"jpeg-bytes", image_format="jpeg"))
        )
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        await toolset.generate_image("a cat")

        _, filename = static_files.saved[0]
        assert filename.endswith(".jpeg")

    async def test_raises_on_http_error(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        patch_transport.responses.append(httpx.Response(500, json={"error": "boom"}))
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        # A Cloud failure becomes a ModelRetry so the agent turn survives.
        with pytest.raises(ModelRetry):
            await toolset.generate_image("a cat")
        assert static_files.saved == []

    async def test_raises_on_malformed_response(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        patch_transport.responses.append(httpx.Response(200, json={"unexpected": "shape"}))
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        with pytest.raises(ModelRetry):
            await toolset.generate_image("a cat")
        assert static_files.saved == []

    async def test_raises_when_artifact_not_a_dict(
        self, static_files: _FakeStaticFilesManager, patch_transport: _TransportRecorder
    ) -> None:
        # A JSON body whose `artifact` is the wrong shape must not escape as TypeError.
        patch_transport.responses.append(httpx.Response(200, json={"artifact": ["not", "a", "dict"]}))
        toolset = _make_toolset(ImageGenerationToolsetConfig(api_key="k"), static_files)

        with pytest.raises(ModelRetry):
            await toolset.generate_image("a cat")
        assert static_files.saved == []
