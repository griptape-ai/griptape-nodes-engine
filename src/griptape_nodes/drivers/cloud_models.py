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
from typing import Any, NamedTuple

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


MODEL_CHOICES_ARGS = [
    # Anthropic
    {"name": "claude-sonnet-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-sonnet-5-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-opus-5-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-opus-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-haiku-5-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-haiku-4-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": False},
    # Google
    {"name": "gemini-3.6-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3.5-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3.5-flash-lite", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3.1-pro", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3.1-flash-lite", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-2.5-pro", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-2.5-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-2.5-flash-lite", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    # OpenAI
    {"name": "gpt-6-sol", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-6-luna", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.6-sol", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.6-terra", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.6-luna", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.5", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.4", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.2", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5.2-chat", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": False},
    {"name": "gpt-5.1", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5-mini", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-5-nano", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": False},
    {"name": "gpt-4.1", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-4.1-mini", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-4.1-nano", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "gpt-4o", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "o4-mini", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "o3", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": True},
    {"name": "o3-mini", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": False},
    {"name": "o1", "icon": "logos/openai.svg", "args": _OPENAI_ARGS, "vision": False},
    # Other
    {"name": "deepseek-v3", "icon": "logos/deepseek.svg", "args": _DEEPSEEK_V3_ARGS, "vision": False},
    {"name": "deepseek.r1-v1", "icon": "logos/deepseek.svg", "args": _DEEPSEEK_R1_ARGS, "vision": False},
    {"name": "llama3-3-70b-instruct-v1", "icon": "logos/meta.svg", "args": _LLAMA_ARGS, "vision": False},
    {"name": "llama3-1-70b-instruct-v1", "icon": "logos/meta.svg", "args": _LLAMA_ARGS, "vision": False},
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


IMAGE_MODEL_CHOICES_ARGS = [
    # OpenAI
    {"name": "gpt-image-1.5", "icon": "logos/openai.svg"},
    {"name": "gpt-image-1-mini", "icon": "logos/openai.svg"},
]

IMAGE_MODEL_CHOICES: list[str] = [str(model["name"]) for model in IMAGE_MODEL_CHOICES_ARGS]


# --- Engine model catalog (admin policy) ---


class _EngineCatalogModel(NamedTuple):
    """One Griptape Cloud model's placement in the engine's own model catalog.

    `key` matches the catalog key `griptape-nodes-library-standard` declares for the
    same `provider_model_id` (e.g. ``gtc_claude_sonnet_5``), so one admin rule gates
    both the node dropdown and the chat sidebar. `provider_id` is the vendor that
    serves the call (``anthropic``, ``openai``, ...), never ``griptape_cloud`` --
    see "One model, several providers" in the design doc.
    """

    key: str
    provider_id: str
    display_name: str
    family: str
    key_support: KeySupport


# Keyed by `provider_model_id` (the chat/image model's wire name) so
# `_build_engine_model_catalog` and `AccessManager`'s chat resolution can both look
# a model up by the name the sidebar actually sends.
_CHAT_MODEL_CATALOG: dict[str, _EngineCatalogModel] = {
    # Anthropic
    "claude-sonnet-5": _EngineCatalogModel(
        "gtc_claude_sonnet_5",
        "anthropic",
        "Claude Sonnet 5",
        "Claude 5",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
    "claude-sonnet-5-5": _EngineCatalogModel(
        "gtc_claude_sonnet_5_5",
        "anthropic",
        "Claude Sonnet 5.5",
        "Claude 5",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
    "claude-opus-5-5": _EngineCatalogModel(
        "gtc_claude_opus_5_5",
        "anthropic",
        "Claude Opus 5.5",
        "Claude 5",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
    "claude-opus-5": _EngineCatalogModel(
        "gtc_claude_opus_5", "anthropic", "Claude Opus 5", "Claude 5", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "claude-haiku-5-5": _EngineCatalogModel(
        "gtc_claude_haiku_5_5",
        "anthropic",
        "Claude Haiku 5.5",
        "Claude 5",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
    "claude-haiku-4-5": _EngineCatalogModel(
        "gtc_claude_haiku_4_5",
        "anthropic",
        "Claude Haiku 4.5",
        "Claude 4",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
    # Google
    "gemini-3.6-flash": _EngineCatalogModel(
        "gtc_gemini_3_6_flash", "google", "Gemini 3.6 Flash", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-3.5-flash": _EngineCatalogModel(
        "gtc_gemini_3_5_flash", "google", "Gemini 3.5 Flash", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-3.5-flash-lite": _EngineCatalogModel(
        "gtc_gemini_3_5_flash_lite", "google", "Gemini 3.5 Flash-Lite", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-3.1-pro": _EngineCatalogModel(
        "gtc_gemini_3_1_pro", "google", "Gemini 3.1 Pro", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-3.1-flash-lite": _EngineCatalogModel(
        "gtc_gemini_3_1_flash_lite", "google", "Gemini 3.1 Flash-Lite", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-3-flash": _EngineCatalogModel(
        "gtc_gemini_3_flash", "google", "Gemini 3 Flash", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-2.5-pro": _EngineCatalogModel(
        "gtc_gemini_2_5_pro", "google", "Gemini 2.5 Pro", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-2.5-flash": _EngineCatalogModel(
        "gtc_gemini_2_5_flash", "google", "Gemini 2.5 Flash", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gemini-2.5-flash-lite": _EngineCatalogModel(
        "gtc_gemini_2_5_flash_lite", "google", "Gemini 2.5 Flash-Lite", "Gemini", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    # OpenAI
    "gpt-6-sol": _EngineCatalogModel(
        "gtc_gpt_6_sol", "openai", "GPT-6 Sol", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-6-luna": _EngineCatalogModel(
        "gtc_gpt_6_luna", "openai", "GPT-6 Luna", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.6-sol": _EngineCatalogModel(
        "gtc_gpt_5_6_sol", "openai", "GPT-5.6 Sol", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.6-terra": _EngineCatalogModel(
        "gtc_gpt_5_6_terra", "openai", "GPT-5.6 Terra", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.6-luna": _EngineCatalogModel(
        "gtc_gpt_5_6_luna", "openai", "GPT-5.6 Luna", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.5": _EngineCatalogModel(
        "gtc_gpt_5_5", "openai", "GPT-5.5", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.4": _EngineCatalogModel(
        "gtc_gpt_5_4", "openai", "GPT-5.4", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.2": _EngineCatalogModel(
        "gtc_gpt_5_2", "openai", "GPT-5.2", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5.2-chat": _EngineCatalogModel(
        "gtc_gpt_5_2_chat", "openai", "GPT-5.2 Chat", "GPT", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "gpt-5.1": _EngineCatalogModel(
        "gtc_gpt_5_1", "openai", "GPT-5.1", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5": _EngineCatalogModel(
        "gtc_gpt_5", "openai", "GPT-5", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5-mini": _EngineCatalogModel(
        "gtc_gpt_5_mini", "openai", "GPT-5 mini", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-5-nano": _EngineCatalogModel(
        "gtc_gpt_5_nano", "openai", "GPT-5 nano", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-4.1": _EngineCatalogModel(
        "gtc_gpt_4_1", "openai", "GPT-4.1", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-4.1-mini": _EngineCatalogModel(
        "gtc_gpt_4_1_mini", "openai", "GPT-4.1 mini", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-4.1-nano": _EngineCatalogModel(
        "gtc_gpt_4_1_nano", "openai", "GPT-4.1 nano", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-4o": _EngineCatalogModel(
        "gtc_gpt_4o", "openai", "GPT-4o", "GPT", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "o4-mini": _EngineCatalogModel(
        "gtc_o4_mini", "openai", "o4 mini", "o-series", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "o3": _EngineCatalogModel("gtc_o3", "openai", "o3", "o-series", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY),
    "o3-mini": _EngineCatalogModel(
        "gtc_o3_mini", "openai", "o3 mini", "o-series", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "o1": _EngineCatalogModel("gtc_o1", "openai", "o1", "o-series", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY),
    # DeepSeek
    "deepseek-v3": _EngineCatalogModel(
        "gtc_deepseek_v3", "deepseek", "DeepSeek V3", "DeepSeek V3", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "deepseek.r1-v1": _EngineCatalogModel(
        "gtc_deepseek_r1", "deepseek", "DeepSeek R1", "DeepSeek R1", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    # Meta
    "llama3-3-70b-instruct-v1": _EngineCatalogModel(
        "gtc_llama_3_3_70b", "meta", "Llama 3.3 70B Instruct", "Llama 3", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
    "llama3-1-70b-instruct-v1": _EngineCatalogModel(
        "gtc_llama_3_1_70b", "meta", "Llama 3.1 70B Instruct", "Llama 3", KeySupport.REQUIRES_GRIPTAPE_KEY
    ),
}

_IMAGE_MODEL_CATALOG: dict[str, _EngineCatalogModel] = {
    "gpt-image-1.5": _EngineCatalogModel(
        "gtc_gpt_image_1_5", "openai", "GPT Image 1.5", "GPT Image", KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY
    ),
    "gpt-image-1-mini": _EngineCatalogModel(
        "gtc_gpt_image_1_mini",
        "openai",
        "GPT Image 1 Mini",
        "GPT Image",
        KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
    ),
}

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

    Every Griptape Cloud chat and image model gets the same key, provider, and
    family `griptape-nodes-library-standard` declares for the same
    `provider_model_id`, so a rule written for a node also gates the same model in
    chat. `ollama`, `lmstudio`, and `custom_endpoint` declare no fixed models --
    their models are discovered at runtime and resolve to a derived id instead
    (see `AccessManager.resolve_chat_model`).
    """
    providers: dict[str, ModelProvider] = {
        provider_id: ModelProvider(display_name=display_name)
        for provider_id, display_name in _VENDOR_PROVIDER_DISPLAY_NAMES.items()
    }
    for catalog in (_CHAT_MODEL_CATALOG, _IMAGE_MODEL_CATALOG):
        for provider_model_id, entry in catalog.items():
            providers[entry.provider_id].models[entry.key] = Model(
                display_name=entry.display_name,
                provider_model_id=provider_model_id,
                family=entry.family,
                key_support=entry.key_support,
            )
    providers[ProviderID.OLLAMA] = ModelProvider(display_name="Ollama (local)", key_support=KeySupport.NO_KEY_REQUIRED)
    providers[ProviderID.LMSTUDIO] = ModelProvider(
        display_name="LM Studio (local)", key_support=KeySupport.NO_KEY_REQUIRED
    )
    providers[CUSTOM_ENDPOINT_PROVIDER_ID] = ModelProvider(
        display_name="Custom (OpenAI-compatible)", key_support=KeySupport.REQUIRES_CUSTOMER_KEY
    )
    return ModelCatalogLibraryProperty(providers=providers)


ENGINE_MODEL_CATALOG = _build_engine_model_catalog()


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

# Source of truth for the sidebar's provider catalog.
# Provider IDs here must match the model_catalog declaration keys used in
# griptape-nodes-library-standard so that admin enforcement applies uniformly
# when the enforcement PR lands.
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
