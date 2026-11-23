"""Canonical catalog of Griptape Cloud-backed chat and image models.

This module is the single source of truth for every consumer that offers a
Griptape Cloud model dropdown: nodes in `griptape-nodes-library-standard`
(Agent, GriptapeCloudPrompt, GriptapeCloudImage, etc.) re-export these
constants, and the engine's `agent_manager` serves them to the chat sidebar
via `ListAgentModelsRequest`.

It mirrors the active `model_type=chat` / `model_type=image` rows in
Griptape Cloud's ServiceModelConfig table. When Cloud's catalog changes
(new model added, deprecated model deactivated), update this file and
every consumer picks up the change.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    Model,
    ModelCatalogLibraryProperty,
    ModelCatalogSidebarProperty,
    ModelProvider,
    SidebarModelProvider,
)


class ProviderID(StrEnum):
    GRIPTAPE_CLOUD = "griptape_cloud"
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    CUSTOM = "custom"


# --- Per-family arg presets ---

_CLAUDE_ARGS = {"stream": True, "structured_output_strategy": "tool", "max_tokens": 64000}
_DEEPSEEK_R1_ARGS = {"stream": False, "structured_output_strategy": "tool", "top_p": None}
_DEEPSEEK_V3_ARGS = {"stream": True, "structured_output_strategy": "tool"}
_LLAMA_ARGS = {"stream": True, "structured_output_strategy": "tool"}
_GEMINI_ARGS = {"stream": True}
_OPENAI_ARGS = {"stream": True}


# `key`, `provider`, `display_name`, `family`, and `key_support` are the
# engine's own model-catalog placement for this entry (see
# `_build_engine_model_catalog`), matched to the key `griptape-nodes-library-standard`
# declares for the same `provider_model_id` (`name`, below) so one admin rule
# gates both the node dropdown and the chat sidebar. `provider` is the vendor
# that serves the call (`anthropic`, `openai`, ...), never `griptape_cloud` --
# see "One model, several providers" in the design doc. Only `name`, `icon`,
# `args`, and `vision` are read by griptape-nodes-library-standard.
MODEL_CHOICES_ARGS = [
    # Anthropic
    {
        "name": "claude-sonnet-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": True,
        "key": "gtc_claude_sonnet_5",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5",
        "family": "Claude 5",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "claude-sonnet-5-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": True,
        "key": "gtc_claude_sonnet_5_5",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5.5",
        "family": "Claude 5",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "claude-opus-5-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": True,
        "key": "gtc_claude_opus_5_5",
        "provider": "anthropic",
        "display_name": "Claude Opus 5.5",
        "family": "Claude 5",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "claude-opus-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": True,
        "key": "gtc_claude_opus_5",
        "provider": "anthropic",
        "display_name": "Claude Opus 5",
        "family": "Claude 5",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "claude-haiku-5-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": True,
        "key": "gtc_claude_haiku_5_5",
        "provider": "anthropic",
        "display_name": "Claude Haiku 5.5",
        "family": "Claude 5",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "claude-haiku-4-5",
        "icon": "logos/anthropic.svg",
        "args": _CLAUDE_ARGS,
        "vision": False,
        "key": "gtc_claude_haiku_4_5",
        "provider": "anthropic",
        "display_name": "Claude Haiku 4.5",
        "family": "Claude 4",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    # Google
    {
        "name": "gemini-3.6-flash",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_6_flash",
        "provider": "google",
        "display_name": "Gemini 3.6 Flash",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-3.5-flash",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_5_flash",
        "provider": "google",
        "display_name": "Gemini 3.5 Flash",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-3.5-flash-lite",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_5_flash_lite",
        "provider": "google",
        "display_name": "Gemini 3.5 Flash-Lite",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-3.1-pro",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_1_pro",
        "provider": "google",
        "display_name": "Gemini 3.1 Pro",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-3.1-flash-lite",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_1_flash_lite",
        "provider": "google",
        "display_name": "Gemini 3.1 Flash-Lite",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-3-flash",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_3_flash",
        "provider": "google",
        "display_name": "Gemini 3 Flash",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-2.5-pro",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_2_5_pro",
        "provider": "google",
        "display_name": "Gemini 2.5 Pro",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-2.5-flash",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_2_5_flash",
        "provider": "google",
        "display_name": "Gemini 2.5 Flash",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gemini-2.5-flash-lite",
        "icon": "logos/google.svg",
        "args": _GEMINI_ARGS,
        "vision": True,
        "key": "gtc_gemini_2_5_flash_lite",
        "provider": "google",
        "display_name": "Gemini 2.5 Flash-Lite",
        "family": "Gemini",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    # OpenAI
    {
        "name": "gpt-6-sol",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_6_sol",
        "provider": "openai",
        "display_name": "GPT-6 Sol",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-6-luna",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_6_luna",
        "provider": "openai",
        "display_name": "GPT-6 Luna",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.6-sol",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_6_sol",
        "provider": "openai",
        "display_name": "GPT-5.6 Sol",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.6-terra",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_6_terra",
        "provider": "openai",
        "display_name": "GPT-5.6 Terra",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.6-luna",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_6_luna",
        "provider": "openai",
        "display_name": "GPT-5.6 Luna",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.5",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_5",
        "provider": "openai",
        "display_name": "GPT-5.5",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.4",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_4",
        "provider": "openai",
        "display_name": "GPT-5.4",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.2",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_2",
        "provider": "openai",
        "display_name": "GPT-5.2",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.2-chat",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": False,
        "key": "gtc_gpt_5_2_chat",
        "provider": "openai",
        "display_name": "GPT-5.2 Chat",
        "family": "GPT",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5.1",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_1",
        "provider": "openai",
        "display_name": "GPT-5.1",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5",
        "provider": "openai",
        "display_name": "GPT-5",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5-mini",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_5_mini",
        "provider": "openai",
        "display_name": "GPT-5 mini",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-5-nano",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": False,
        "key": "gtc_gpt_5_nano",
        "provider": "openai",
        "display_name": "GPT-5 nano",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-4.1",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_4_1",
        "provider": "openai",
        "display_name": "GPT-4.1",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-4.1-mini",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_4_1_mini",
        "provider": "openai",
        "display_name": "GPT-4.1 mini",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-4.1-nano",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_4_1_nano",
        "provider": "openai",
        "display_name": "GPT-4.1 nano",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-4o",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_gpt_4o",
        "provider": "openai",
        "display_name": "GPT-4o",
        "family": "GPT",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "o4-mini",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_o4_mini",
        "provider": "openai",
        "display_name": "o4 mini",
        "family": "o-series",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "o3",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": True,
        "key": "gtc_o3",
        "provider": "openai",
        "display_name": "o3",
        "family": "o-series",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "o3-mini",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": False,
        "key": "gtc_o3_mini",
        "provider": "openai",
        "display_name": "o3 mini",
        "family": "o-series",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "o1",
        "icon": "logos/openai.svg",
        "args": _OPENAI_ARGS,
        "vision": False,
        "key": "gtc_o1",
        "provider": "openai",
        "display_name": "o1",
        "family": "o-series",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    # Other
    {
        "name": "deepseek-v3",
        "icon": "logos/deepseek.svg",
        "args": _DEEPSEEK_V3_ARGS,
        "vision": False,
        "key": "gtc_deepseek_v3",
        "provider": "deepseek",
        "display_name": "DeepSeek V3",
        "family": "DeepSeek V3",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "deepseek.r1-v1",
        "icon": "logos/deepseek.svg",
        "args": _DEEPSEEK_R1_ARGS,
        "vision": False,
        "key": "gtc_deepseek_r1",
        "provider": "deepseek",
        "display_name": "DeepSeek R1",
        "family": "DeepSeek R1",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "llama3-3-70b-instruct-v1",
        "icon": "logos/meta.svg",
        "args": _LLAMA_ARGS,
        "vision": False,
        "key": "gtc_llama_3_3_70b",
        "provider": "meta",
        "display_name": "Llama 3.3 70B Instruct",
        "family": "Llama 3",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
    {
        "name": "llama3-1-70b-instruct-v1",
        "icon": "logos/meta.svg",
        "args": _LLAMA_ARGS,
        "vision": False,
        "key": "gtc_llama_3_1_70b",
        "provider": "meta",
        "display_name": "Llama 3.1 70B Instruct",
        "family": "Llama 3",
        "key_support": KeySupport.REQUIRES_GRIPTAPE_KEY,
    },
]

MODEL_CHOICES: list[str] = [str(model["name"]) for model in MODEL_CHOICES_ARGS]
VISION_MODEL_CHOICES: list[str] = [str(model["name"]) for model in MODEL_CHOICES_ARGS if model.get("vision")]


class _PresetKeyKind(StrEnum):
    """What a key in a MODEL_CHOICES_ARGS `args` preset is for."""

    FORWARDED = "forwarded"
    """Maps onto a Pydantic AI `ModelSettings` field, so it reaches the model."""

    DRIVER_ONLY = "driver_only"
    """Configures our driver instead. `ModelSettings` has no slot for it."""


# Every key any preset may carry, and which of the two it is. One mapping rather
# than two lists, because the useful property is that the classification is
# *total*: forwarding is an allowlist, so a key that is a perfectly valid
# ModelSettings field but missing here gets dropped on the way to the wire —
# the same shape of bug MODEL_SETTINGS exists to fix. An unclassified key fails
# `test_every_preset_key_is_classified`, so adding one to a preset forces the
# decision here rather than letting it silently go nowhere. Marking a key
# DRIVER_ONLY is that decision made deliberately, which is what distinguishes it
# from a key that was forgotten.
_PRESET_KEY_KINDS: dict[str, _PresetKeyKind] = {
    "max_tokens": _PresetKeyKind.FORWARDED,
    "temperature": _PresetKeyKind.FORWARDED,
    "top_p": _PresetKeyKind.FORWARDED,
    "stream": _PresetKeyKind.DRIVER_ONLY,
    "structured_output_strategy": _PresetKeyKind.DRIVER_ONLY,
}

MODEL_SETTINGS: dict[str, dict[str, Any]] = {
    str(model["name"]): settings
    for model in MODEL_CHOICES_ARGS
    if (
        settings := {
            key: value
            for key, value in dict(model["args"]).items()  # type: ignore[call-overload]
            if _PRESET_KEY_KINDS.get(key) is _PresetKeyKind.FORWARDED and value is not None
        }
    )
}
"""Per-model Pydantic AI ``ModelSettings``, distilled from :data:`MODEL_CHOICES_ARGS`.

A model whose preset carries no settings-relevant keys is absent rather than
mapped to an empty dict, so "unknown model" and "nothing to apply" collapse into
one case. A ``None`` in a preset means "don't send this field" — today only
``deepseek.r1-v1``'s ``top_p`` — which omitting it already achieves, so those are
filtered out too. (The o-series also rejects ``top_p``, but its preset simply
never sets it; see :data:`O_SERIES_MODELS`.)

Resolves to the Claude models today, since they are the only entries whose
presets carry a ``ModelSettings`` key. Every other catalog model has no preset
setting to apply and keeps the provider default.
"""


def model_settings_for(model_name: str) -> dict[str, Any] | None:
    """Return a copy of the ``ModelSettings`` for a model id, or ``None`` if it has none.

    Args:
        model_name: A Griptape Cloud model id, e.g. ``"claude-opus-5"``. Ids
            outside the catalog — a local Ollama model, a custom endpoint's
            model — have no preset and return ``None``.
    """
    settings = MODEL_SETTINGS.get(model_name)
    if not settings:
        return None
    # Copy: callers pass this into a model instance that may outlive the call,
    # and the catalog dict is module-level shared state.
    return dict(settings)


# See the comment above MODEL_CHOICES_ARGS: `key`, `provider`, `display_name`,
# `family`, and `key_support` are the engine catalog's placement for this entry.
IMAGE_MODEL_CHOICES_ARGS = [
    # OpenAI
    {
        "name": "gpt-image-1.5",
        "icon": "logos/openai.svg",
        "key": "gtc_gpt_image_1_5",
        "provider": "openai",
        "display_name": "GPT Image 1.5",
        "family": "GPT Image",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
    {
        "name": "gpt-image-1-mini",
        "icon": "logos/openai.svg",
        "key": "gtc_gpt_image_1_mini",
        "provider": "openai",
        "display_name": "GPT Image 1 Mini",
        "family": "GPT Image",
        "key_support": KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    },
]

IMAGE_MODEL_CHOICES: list[str] = [str(model["name"]) for model in IMAGE_MODEL_CHOICES_ARGS]


# Maps deprecated model IDs that may appear in saved workflows to their live
# replacement. Consumers use this to rewrite the model on load and surface a
# deprecation notice to the user.
DEPRECATED_MODELS = {
    # Anthropic
    "claude-3-7-sonnet": "claude-sonnet-5",
    "claude-3-5-haiku": "claude-haiku-4-5",
    "claude-sonnet-4-20250514": "claude-sonnet-5",
    "claude-4-5-sonnet": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-5",
    "claude-opus-4-7": "claude-opus-5",
    # Bedrock
    "amazon.titan-text-premier-v1": "claude-sonnet-5",
    # Azure OpenAI
    "gpt-4.5-preview": "gpt-4.1",
    "o1-mini": "o3-mini",
    # Google
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20": "gemini-2.5-flash",
    "gemini-2.5-pro-preview-06-05": "gemini-2.5-pro",
    "gemini-3-pro": "gemini-3.1-pro",
    "gemini-3-pro-preview": "gemini-3.1-pro",
}


# Maps deprecated image model IDs that may appear in saved workflows to their
# live replacement. Mirrors DEPRECATED_MODELS but for the image catalog.
IMAGE_DEPRECATED_MODELS = {
    "dall-e-3": "gpt-image-1-mini",
    "gpt-image-1": "gpt-image-1-mini",
}


# Model IDs whose backend does not accept top_p (the OpenAI o-series).
# Kept in sync with the o-entries in MODEL_CHOICES_ARGS.
O_SERIES_MODELS = {"o1", "o3", "o3-mini", "o4-mini"}


OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
LM_STUDIO_DEFAULT_BASE_URL = "http://localhost:1234/v1"

# Source of truth for the sidebar's provider catalog. These ids are the
# sidebar's connection types (how the chat UI talks to a provider), which is a
# separate concern from the catalog providers license policy matches against
# (`ENGINE_MODEL_CATALOG`, built below from these entries' `display_name` /
# `key_support` for the local and custom ones).
PROVIDER_CATALOG = ModelCatalogSidebarProperty(
    providers={
        ProviderID.GRIPTAPE_CLOUD: SidebarModelProvider(
            display_name="Griptape Cloud",
            terms_url="https://www.griptape.ai/legal/terms",
            notes="Routes upstream models through Griptape's hosted proxy.",
            key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
            default_base_url=None,
            has_model_list=True,
            default_model=MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o",
        ),
        ProviderID.OLLAMA: SidebarModelProvider(
            display_name="Ollama (local)",
            terms_url="https://ollama.com/terms",
            key_support=KeySupport.NO_KEY_REQUIRED,
            notes="Models are dynamically discovered from the local Ollama installation.",
            default_base_url=OLLAMA_DEFAULT_BASE_URL,
            has_model_list=False,
            default_model="llama3.2",
        ),
        ProviderID.LMSTUDIO: SidebarModelProvider(
            display_name="LM Studio (local)",
            terms_url="https://lmstudio.ai/app-terms",
            key_support=KeySupport.NO_KEY_REQUIRED,
            notes="Models are dynamically discovered from the local LM Studio installation.",
            default_base_url=LM_STUDIO_DEFAULT_BASE_URL,
            has_model_list=False,
            default_model="",
        ),
        ProviderID.CUSTOM: SidebarModelProvider(
            display_name="Custom (OpenAI-compatible)",
            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
            default_base_url="",
            has_model_list=False,
            default_model="",
        ),
    }
)


class ProviderCatalogEntry(BaseModel):
    """Serialization-ready response shape for a single provider catalog entry."""

    id: str
    display_name: str
    terms_url: str | None = None
    key_support: KeySupport | None = None
    notes: str | None = None
    requires_api_key: bool
    default_base_url: str | None = None
    has_model_list: bool = False
    default_model: str = ""


def provider_accepts_customer_key(provider_id: str) -> bool:
    """Return True only if this provider expects the user to supply their own API key."""
    provider = PROVIDER_CATALOG.providers.get(provider_id)
    return provider is not None and provider.key_support == KeySupport.REQUIRES_CUSTOMER_KEY


def provider_catalog_entries() -> list[ProviderCatalogEntry]:
    """Return the full provider list for the ListAgentModelsResultSuccess response.

    Each entry includes catalog fields and sidebar-specific fields.
    requires_api_key is a convenience bool so the frontend doesn't have to
    parse key_support itself.
    """
    return [
        ProviderCatalogEntry(
            id=provider_id,
            requires_api_key=provider.key_support == KeySupport.REQUIRES_CUSTOMER_KEY,
            **provider.model_dump(exclude={"models"}),
        )
        for provider_id, provider in PROVIDER_CATALOG.providers.items()
    ]


# --- Engine model catalog (admin policy) ---

_VENDOR_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic",
    "google": "Google",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "meta": "Meta",
}

# The catalog provider a `custom` connection's models resolve under. `custom` stays
# the sidebar's connection type (see `ProviderID`); `custom_endpoint` is what a
# policy matches, since nothing verifies where the endpoint actually points.
CUSTOM_ENDPOINT_PROVIDER_ID = "custom_endpoint"


def _build_engine_model_catalog() -> ModelCatalogLibraryProperty:
    """Build the engine's own model catalog: Griptape Cloud models under their vendor, plus local/custom providers.

    Every Griptape Cloud chat and image model gets the key, provider, and
    family carried on its `MODEL_CHOICES_ARGS` / `IMAGE_MODEL_CHOICES_ARGS`
    entry, matching what `griptape-nodes-library-standard` declares for the
    same `provider_model_id`, so a rule written for a node also gates the same
    model in chat. `ollama`, `lmstudio`, and `custom_endpoint` declare no fixed
    models -- their models are discovered at runtime and resolve to a derived
    id instead (see `AccessManager.resolve_chat_model`) -- so they only need
    the display name and key support `PROVIDER_CATALOG` already declares for
    the sidebar.
    """
    providers: dict[str, ModelProvider] = {
        provider_id: ModelProvider(display_name=display_name)
        for provider_id, display_name in _VENDOR_PROVIDER_DISPLAY_NAMES.items()
    }
    for catalog in (MODEL_CHOICES_ARGS, IMAGE_MODEL_CHOICES_ARGS):
        for entry in catalog:
            providers[str(entry["provider"])].models[str(entry["key"])] = Model(
                display_name=str(entry["display_name"]),
                provider_model_id=str(entry["name"]),
                family=str(entry["family"]),
                key_support=entry["key_support"],  # type: ignore[arg-type]
            )
    for provider_id in (ProviderID.OLLAMA, ProviderID.LMSTUDIO):
        sidebar_provider = PROVIDER_CATALOG.providers[provider_id]
        providers[provider_id] = ModelProvider(
            display_name=sidebar_provider.display_name, key_support=sidebar_provider.key_support
        )
    custom_sidebar_provider = PROVIDER_CATALOG.providers[ProviderID.CUSTOM]
    providers[CUSTOM_ENDPOINT_PROVIDER_ID] = ModelProvider(
        display_name=custom_sidebar_provider.display_name, key_support=custom_sidebar_provider.key_support
    )
    return ModelCatalogLibraryProperty(providers=providers)


ENGINE_MODEL_CATALOG = _build_engine_model_catalog()
