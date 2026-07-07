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

from pydantic import BaseModel

from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    ModelCatalogSidebarProperty,
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
    {"name": "claude-opus-4-7", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-sonnet-4-6", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-4-5-sonnet", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": True},
    {"name": "claude-haiku-4-5", "icon": "logos/anthropic.svg", "args": _CLAUDE_ARGS, "vision": False},
    # Google
    {"name": "gemini-3.1-pro", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-3.1-flash-lite", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": False},
    {"name": "gemini-3-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": False},
    {"name": "gemini-2.5-pro", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": True},
    {"name": "gemini-2.5-flash", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": False},
    {"name": "gemini-2.5-flash-lite", "icon": "logos/google.svg", "args": _GEMINI_ARGS, "vision": False},
    # OpenAI
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


IMAGE_MODEL_CHOICES_ARGS = [
    # OpenAI
    {"name": "gpt-image-1.5", "icon": "logos/openai.svg"},
    {"name": "gpt-image-1-mini", "icon": "logos/openai.svg"},
]

IMAGE_MODEL_CHOICES: list[str] = [str(model["name"]) for model in IMAGE_MODEL_CHOICES_ARGS]


# Maps deprecated model IDs that may appear in saved workflows to their live
# replacement. Consumers use this to rewrite the model on load and surface a
# deprecation notice to the user.
DEPRECATED_MODELS = {
    # Anthropic
    "claude-3-7-sonnet": "claude-sonnet-4-6",
    "claude-3-5-haiku": "claude-haiku-4-5",
    "claude-sonnet-4-20250514": "claude-sonnet-4-6",
    # Bedrock
    "amazon.titan-text-premier-v1": "claude-sonnet-4-6",
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
