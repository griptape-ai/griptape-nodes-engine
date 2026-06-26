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

from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    ModelCatalogLibraryProperty,
    ModelProvider,
)

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
PROVIDER_CATALOG = ModelCatalogLibraryProperty(
    providers={
        "griptape_cloud": ModelProvider(
            display_name="Griptape Cloud",
            terms_url="https://www.griptape.ai/legal/terms",
            notes="Routes upstream models through Griptape's hosted proxy.",
            key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
        ),
        "ollama": ModelProvider(
            display_name="Ollama (local)",
            terms_url="https://ollama.com/terms",
            key_support=KeySupport.NO_KEY_REQUIRED,
            notes="Models are dynamically discovered from the local Ollama installation.",
        ),
        "lmstudio": ModelProvider(
            display_name="LM Studio (local)",
            terms_url="https://lmstudio.ai/app-terms",
            key_support=KeySupport.NO_KEY_REQUIRED,
            notes="Models are dynamically discovered from the local LM Studio installation.",
        ),
        "custom": ModelProvider(
            display_name="Custom (OpenAI-compatible)",
            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
        ),
    }
)

# Sidebar-specific fields that have no ModelProvider equivalent.
# default_base_url: pre-filled URL (None = use engine default for that provider)
# has_model_list: True = show the curated MODEL_CHOICES dropdown; False = freetext
# default_model: value to populate when the user first selects this provider
_SIDEBAR_EXTRA: dict[str, dict] = {
    "griptape_cloud": {
        "default_base_url": None,
        "has_model_list": True,
        "default_model": MODEL_CHOICES[0] if MODEL_CHOICES else "gpt-4o",
    },
    "ollama": {
        "default_base_url": OLLAMA_DEFAULT_BASE_URL,
        "has_model_list": False,
        "default_model": "llama3.2",
    },
    "lmstudio": {
        "default_base_url": LM_STUDIO_DEFAULT_BASE_URL,
        "has_model_list": False,
        "default_model": "",
    },
    "custom": {
        "default_base_url": "",
        "has_model_list": False,
        "default_model": "",
    },
}


def provider_accepts_customer_key(provider_id: str) -> bool:
    """Return True only if this provider expects the user to supply their own API key."""
    provider = PROVIDER_CATALOG.providers.get(provider_id)
    return provider is not None and provider.key_support == KeySupport.REQUIRES_CUSTOMER_KEY


def provider_catalog_entries() -> list[dict]:
    """Return the full provider list for the ListAgentModelsResultSuccess response.

    Each entry merges catalog fields (id, display_name, terms_url, key_support,
    notes) with sidebar-specific fields (default_base_url, has_model_list,
    default_model). requires_api_key is included as a convenience bool so the
    frontend doesn't have to parse key_support itself.
    """
    return [
        {
            "id": provider_id,
            "display_name": provider.display_name,
            "terms_url": provider.terms_url,
            "key_support": str(provider.key_support) if provider.key_support else None,
            "notes": provider.notes,
            "requires_api_key": provider.key_support == KeySupport.REQUIRES_CUSTOMER_KEY,
            **_SIDEBAR_EXTRA[provider_id],
        }
        for provider_id, provider in PROVIDER_CATALOG.providers.items()
    ]
