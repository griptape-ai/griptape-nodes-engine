"""Griptape Cloud image-generation tool for the chat-sidebar agent.

The Griptape ``Agent`` harness exposed a ``BaseImageGenerationTool`` backed by
``GriptapeCloudImageGenerationDriver`` so the chat sidebar could turn a text
prompt into an image. This module ports that capability to the Pydantic AI
harness without depending on Griptape's tool/driver classes: it calls Griptape
Cloud's image-generations endpoint directly over HTTP, persists the returned
image through the engine's static file store, and returns the resulting URL so
the model can reference it in its reply.

The tool registers against a ``pydantic_ai.Agent`` via
:func:`register_image_tools`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from pydantic_ai.exceptions import ModelRetry

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager


logger = logging.getLogger("griptape_nodes")


GRIPTAPE_CLOUD_BASE_URL = "https://cloud.griptape.ai"
"""Default Griptape Cloud root. The ``/api/images`` prefix is added here."""

DEFAULT_IMAGE_MODEL = "gpt-image-1-mini"
DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS = 120.0

IMAGE_TOOL_NAME = "generate_image"
"""Name the image tool registers under. Consumers match tool results against this."""

ALLOWED_IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536")


@dataclass(frozen=True)
class ImageGenerationToolsetConfig:
    """Configuration for the Griptape Cloud image-generation toolset.

    Attributes:
        api_key: Griptape Cloud API key. Sent as ``Authorization: Bearer <key>``.
        model: Image-generation model id (e.g. ``gpt-image-1-mini``). Cloud
            picks the underlying provider from this name server-side.
        base_url: Griptape Cloud root URL (no ``/api`` suffix).
        image_size: Optional output size. When set, must be one of
            :data:`ALLOWED_IMAGE_SIZES`.
        quality: Optional quality level (``low``, ``medium``, ``high``).
        background: Optional background (``transparent``, ``opaque``, ``auto``).
        output_format: Optional output format (``png`` or ``jpeg``).
        timeout_seconds: Hard wall-clock cap on each generation request.
    """

    api_key: str
    model: str = DEFAULT_IMAGE_MODEL
    base_url: str = GRIPTAPE_CLOUD_BASE_URL
    image_size: str | None = None
    quality: str | None = None
    background: str | None = None
    output_format: str | None = None
    timeout_seconds: float = DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.api_key:
            msg = "ImageGenerationToolsetConfig requires a non-empty `api_key`."
            raise ValueError(msg)
        if self.image_size is not None and self.image_size not in ALLOWED_IMAGE_SIZES:
            msg = f"Image size {self.image_size!r} must be one of {ALLOWED_IMAGE_SIZES}."
            raise ValueError(msg)


class ImageGenerationToolset:
    """Owns the Griptape Cloud image config and exposes the agent-facing tool."""

    def __init__(self, config: ImageGenerationToolsetConfig, static_files_manager: StaticFilesManager) -> None:
        self._config = config
        self._static_files_manager = static_files_manager
        self._base_url = config.base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {config.api_key}"}

    @property
    def model(self) -> str:
        return self._config.model

    def register_on(self, agent: Agent) -> None:
        """Register the image-generation tool on the given Pydantic AI agent."""
        agent.tool_plain(self.generate_image)

    async def generate_image(self, prompt: str, negative_prompt: str = "") -> str:
        """Generate an image from a text prompt and return its workspace URL.

        Args:
            prompt: Text description of the image to generate.
            negative_prompt: Optional description of what to avoid. Sent to the
                model when non-empty.

        Returns:
            A URL pointing at the saved image inside the user's static file
            store. Reference this URL in your reply so the user can view it.
        """
        if not prompt or not prompt.strip():
            msg = "generate_image requires a non-empty `prompt`."
            raise ModelRetry(msg)

        payload: dict[str, object] = {
            "prompts": [prompt],
            "driver_configuration": self._driver_configuration(),
        }
        if negative_prompt.strip():
            payload["negative_prompts"] = [negative_prompt]

        url = f"{self._base_url}/api/images/generations"
        # Recoverable Cloud/parse failures are surfaced as ModelRetry so a single
        # bad image request does not abort the whole agent turn (and discard any
        # state already mutated by earlier tool calls); the model can retry or
        # explain the failure instead.
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                response = await client.post(url, headers=self._headers, json=payload)
            response.raise_for_status()
            artifact = response.json()["artifact"]
            image_bytes = base64.b64decode(artifact["value"])
            image_format = artifact.get("format", "png")
        except httpx.HTTPError as exc:
            msg = f"Image generation request to Griptape Cloud failed: {exc}"
            raise ModelRetry(msg) from exc
        except (KeyError, ValueError, TypeError) as exc:
            # ValueError covers json.JSONDecodeError and base64 binascii.Error;
            # KeyError/TypeError cover a JSON body whose `artifact` is missing or
            # not the expected dict shape.
            msg = f"Image generation returned an unexpected response: {exc}"
            raise ModelRetry(msg) from exc
        filename = f"{uuid.uuid4()}.{image_format}"

        # `save_static_file` is synchronous disk/network I/O; offload it so the
        # agent's event loop is not blocked while the file is persisted.
        image_url = await asyncio.to_thread(self._static_files_manager.save_static_file, image_bytes, filename)
        logger.info("generate_image saved %d bytes to %s", len(image_bytes), filename)
        return image_url

    def _driver_configuration(self) -> dict[str, str]:
        """Build the non-empty subset of driver options Cloud expects."""
        config = {
            "model": self._config.model,
            "image_size": self._config.image_size,
            "quality": self._config.quality,
            "background": self._config.background,
            "output_format": self._config.output_format,
        }
        return {key: value for key, value in config.items() if value is not None}


def register_image_tools(
    agent: Agent,
    config: ImageGenerationToolsetConfig,
    static_files_manager: StaticFilesManager,
) -> ImageGenerationToolset:
    """Build an :class:`ImageGenerationToolset` and register it on ``agent``.

    Returns the toolset so callers can hold a reference (e.g. for tests, or to
    reuse the same config across multiple agents).
    """
    toolset = ImageGenerationToolset(config, static_files_manager)
    toolset.register_on(agent)
    return toolset
