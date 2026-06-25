"""Build a Pydantic AI model pointed at Griptape Cloud's OpenAI-compatible API.

Griptape Cloud exposes an OpenAI-compatible Chat Completions endpoint at
``POST {base_url}/api/v1/chat/completions``. It translates OpenAI requests into
Griptape's own ``PromptStack`` / ``Message`` shapes and runs them through
whichever provider the configured model maps to (OpenAI, Anthropic, Bedrock,
Google, etc.). Because the wire format is plain OpenAI Chat Completions, we use
Pydantic AI's built-in :class:`OpenAIChatModel` instead of a hand-rolled model
adapter: text, native tool calls, structured output, and streaming usage all
flow through the standard client.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from griptape_nodes.drivers.cloud_models import LM_STUDIO_DEFAULT_BASE_URL, OLLAMA_DEFAULT_BASE_URL

GRIPTAPE_CLOUD_BASE_URL = "https://cloud.griptape.ai"
"""Default Griptape Cloud root. The ``/api/v1`` OpenAI-compatible prefix is added here."""


def build_griptape_cloud_model(
    model_name: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenAIChatModel:
    """Return an :class:`OpenAIChatModel` bound to Griptape Cloud's ``/api/v1`` endpoint.

    Args:
        model_name: The Griptape Cloud model id (e.g. ``"gpt-4o"``). Cloud picks
            the underlying provider from this name server-side.
        api_key: Griptape Cloud API key. Falls back to the ``GT_CLOUD_API_KEY``
            environment variable. Sent as ``Authorization: Bearer <key>``.
        base_url: Griptape Cloud root URL (no ``/api/v1`` suffix). Falls back to
            the ``GT_CLOUD_BASE_URL`` environment variable, then to
            :data:`GRIPTAPE_CLOUD_BASE_URL`.

    Raises:
        ValueError: If no API key is available.
    """
    resolved_key = api_key or os.environ.get("GT_CLOUD_API_KEY")
    if not resolved_key:
        msg = "Griptape Cloud API key is required. Pass `api_key=` or set the GT_CLOUD_API_KEY environment variable."
        raise ValueError(msg)

    cloud_root = (base_url or os.environ.get("GT_CLOUD_BASE_URL", GRIPTAPE_CLOUD_BASE_URL)).rstrip("/")
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=f"{cloud_root}/api/v1", api_key=resolved_key),
    )


def build_model(
    model_name: str,
    *,
    provider: str = "griptape_cloud",
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenAIChatModel:
    """Return an :class:`OpenAIChatModel` for the given provider.

    Args:
        model_name: Model identifier sent to the API.
        provider: One of ``"griptape_cloud"``, ``"ollama"``, ``"lmstudio"``,
            or ``"custom"``.
        api_key: API key for the target endpoint. Required for
            ``"griptape_cloud"`` (falls back to ``GT_CLOUD_API_KEY``) and
            ``"custom"``. Ignored for ``"ollama"`` and ``"lmstudio"``
            (no auth needed).
        base_url: Base URL of the endpoint. For ``"griptape_cloud"`` the
            ``/api/v1`` suffix is appended automatically. For ``"ollama"``
            defaults to :data:`OLLAMA_DEFAULT_BASE_URL`. For ``"lmstudio"``
            defaults to :data:`LM_STUDIO_DEFAULT_BASE_URL`. Required for
            ``"custom"``.

    Raises:
        ValueError: If required credentials or URLs are missing.
    """
    if provider == "griptape_cloud":
        return build_griptape_cloud_model(model_name, api_key=api_key, base_url=base_url)

    if provider == "ollama":
        resolved_url = (base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
        # Ollama doesn't require auth but the OpenAI client needs a non-empty key.
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=resolved_url, api_key="ollama"),
        )

    if provider == "lmstudio":
        resolved_url = (base_url or LM_STUDIO_DEFAULT_BASE_URL).rstrip("/")
        # LM Studio doesn't require auth but the OpenAI client needs a non-empty key.
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=resolved_url, api_key="lm-studio"),
        )

    # "custom" or any future provider: caller must supply both url and key.
    if not base_url:
        msg = f"base_url is required for provider '{provider}'."
        raise ValueError(msg)
    if not api_key:
        msg = f"api_key is required for provider '{provider}'."
        raise ValueError(msg)
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url.rstrip("/"), api_key=api_key),
    )
