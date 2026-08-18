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
from typing import TYPE_CHECKING, cast

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from griptape_nodes.drivers.cloud_credentials import MISSING_CREDENTIAL_MESSAGE, resolve_cloud_credential
from griptape_nodes.drivers.cloud_models import (
    LM_STUDIO_DEFAULT_BASE_URL,
    OLLAMA_DEFAULT_BASE_URL,
    ProviderID,
    model_settings_for,
)

if TYPE_CHECKING:
    from pydantic_ai.settings import ModelSettings

GRIPTAPE_CLOUD_BASE_URL = "https://cloud.griptape.ai"
"""Default Griptape Cloud root. The ``/api/v1`` OpenAI-compatible prefix is added here."""


def build_griptape_cloud_model(
    model_name: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    settings: ModelSettings | None = None,
) -> OpenAIChatModel:
    """Return an :class:`OpenAIChatModel` bound to Griptape Cloud's ``/api/v1`` endpoint.

    Args:
        model_name: The Griptape Cloud model id (e.g. ``"gpt-4o"``). Cloud picks
            the underlying provider from this name server-side.
        api_key: Griptape Cloud credential. Falls back to the Griptape Nodes
            License, then the ``GT_CLOUD_API_KEY`` environment variable, per
            :func:`resolve_cloud_credential`. Sent as ``Authorization: Bearer <key>``.
        base_url: Griptape Cloud root URL (no ``/api/v1`` suffix). Falls back to
            the ``GT_CLOUD_BASE_URL`` environment variable, then to
            :data:`GRIPTAPE_CLOUD_BASE_URL`.
        settings: Default :class:`ModelSettings` for the returned model. ``None``
            falls back to the catalog preset for ``model_name`` via
            :func:`model_settings_for`; pass a dict to override it. Without
            either, no ``max_tokens`` reaches the wire and the upstream
            provider's own default caps the response — 4096 tokens for
            Anthropic models, well under what the catalog intends.

    Raises:
        ValueError: If neither a license nor an API key is available.
    """
    resolved_key = api_key or resolve_cloud_credential()
    if not resolved_key:
        msg = f"Attempted to reach Griptape Cloud. Failed because {MISSING_CREDENTIAL_MESSAGE}"
        raise ValueError(msg)

    resolved_settings = settings
    if resolved_settings is None:
        preset = model_settings_for(model_name)
        # `cloud_models` is a plain catalog and stays free of pydantic-ai types,
        # so the preset arrives as a plain dict. Its keys are constrained to
        # ModelSettings fields there and asserted in the catalog's tests, so this
        # is the boundary where that guarantee becomes the static type.
        resolved_settings = cast("ModelSettings", preset) if preset is not None else None
    cloud_root = (base_url or os.environ.get("GT_CLOUD_BASE_URL", GRIPTAPE_CLOUD_BASE_URL)).rstrip("/")
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=f"{cloud_root}/api/v1", api_key=resolved_key),
        settings=resolved_settings,
    )


def build_model(
    model_name: str,
    *,
    provider: str = ProviderID.GRIPTAPE_CLOUD,
    api_key: str | None = None,
    base_url: str | None = None,
    settings: ModelSettings | None = None,
) -> OpenAIChatModel:
    """Return an :class:`OpenAIChatModel` for the given provider.

    Args:
        model_name: Model identifier sent to the API.
        provider: One of ``"griptape_cloud"``, ``"ollama"``, ``"lmstudio"``,
            or ``"custom"``.
        api_key: API key for the target endpoint. Required for
            ``"griptape_cloud"`` (falls back to the license, then
            ``GT_CLOUD_API_KEY``) and ``"custom"``. Ignored for ``"ollama"``
            and ``"lmstudio"`` (no auth needed).
        base_url: Base URL of the endpoint. For ``"griptape_cloud"`` the
            ``/api/v1`` suffix is appended automatically. For ``"ollama"``
            defaults to :data:`OLLAMA_DEFAULT_BASE_URL`. For ``"lmstudio"``
            defaults to :data:`LM_STUDIO_DEFAULT_BASE_URL`. Required for
            ``"custom"``.
        settings: Default :class:`ModelSettings` for the returned model, e.g.
            ``{"max_tokens": 64000}``. For ``"griptape_cloud"``, ``None`` falls
            back to the catalog preset for ``model_name``. The other providers
            serve arbitrary model ids that only coincidentally collide with
            catalog names, so they apply ``settings`` verbatim and default to
            sending none.

    Raises:
        ValueError: If required credentials or URLs are missing.
    """
    # Pylance may warn "explicit returns mixed with implicit returns" here, but every
    # branch either raises or returns — case _: is exhaustive. Pyright agrees.
    match provider:
        case ProviderID.GRIPTAPE_CLOUD:
            return build_griptape_cloud_model(model_name, api_key=api_key, base_url=base_url, settings=settings)
        case ProviderID.OLLAMA:
            resolved_url = (base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
            # Ollama doesn't require auth but the OpenAI client needs a non-empty key.
            return OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(base_url=resolved_url, api_key="ollama"),
                settings=settings,
            )
        case ProviderID.LMSTUDIO:
            resolved_url = (base_url or LM_STUDIO_DEFAULT_BASE_URL).rstrip("/")
            # LM Studio doesn't require auth but the OpenAI client needs a non-empty key.
            return OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(base_url=resolved_url, api_key="lm-studio"),
                settings=settings,
            )
        case _:
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
                settings=settings,
            )
