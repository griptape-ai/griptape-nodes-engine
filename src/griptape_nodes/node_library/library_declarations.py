"""Declarative properties and capabilities for libraries and nodes.

Attached to `LibraryMetadata.declarations` and `NodeMetadata.declarations` and
serialized into `griptape_nodes_library.json`.

Most declarations carry a single value -- multi-knob behavior splits into
separate declarations rather than wider models. The `model_catalog` property
is the deliberate exception: it nests a `provider -> model` registry under one
declaration because the levels share identity (a model id is only meaningful
under its provider). Two categories of declaration ship today:

* **Properties** state an identity fact about the library or node.
* **Capabilities** state what the library or node can do.

The `LibraryDeclaration` and `NodeDeclaration` unions are scaffolded so
that additional declarations slot in additively without churning the
schema shape.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# ---------- Shared enums ----------


class LifecycleStage(StrEnum):
    """Lifecycle stage for a library or node. Shared by library- and node-level properties.

    STABLE is an explicit value (not implied by absence) so library authors must
    make a deliberate choice; this lets consumers flag unstated stage as
    "<No lifecycle stage provided by library author>" rather than silently assuming
    STABLE.

    Node-level semantics:
      - Node with no LifecycleStageNodeProperty -> inherits the library's stage.
      - Node with the property -> overrides the library's stage with the declared value.
    """

    STABLE = "STABLE"
    BETA = "BETA"
    ALPHA = "ALPHA"
    LABS = "LABS"
    DEPRECATED = "DEPRECATED"


class KeySupport(StrEnum):
    """What kind of API key authorizes a call to a model.

    Declared on a `Model` (required) and optionally on its `ModelProvider`,
    where it acts as the default for providers that declare no models of their
    own (e.g. a local-runtime provider like Ollama).
    """

    REQUIRES_CUSTOMER_KEY = "REQUIRES_CUSTOMER_KEY"
    SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY = "SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY"
    REQUIRES_GRIPTAPE_KEY = "REQUIRES_GRIPTAPE_KEY"
    NO_KEY_REQUIRED = "NO_KEY_REQUIRED"


class WorkerCompatibility(StrEnum):
    """Whether a library can run in a worker subprocess.

    Absence of a ``WorkerModeCompatibility`` declaration is treated as
    ``COMPATIBLE``.
    """

    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"


class WorkerMode(StrEnum):
    """Where a worker-compatible library launches by default.

    Only meaningful when the library is also ``WorkerCompatibility.COMPATIBLE``.
    Absence of a ``SuggestedWorkerMode`` declaration is treated as
    ``ORCHESTRATOR``.
    """

    ORCHESTRATOR = "ORCHESTRATOR"
    WORKER = "WORKER"


# ---------- Library-level declarations ----------


class LifecycleStageLibraryProperty(BaseModel):
    """Lifecycle stage that applies to every node in the library.

    Absence of this property means "unstated" -- consumers should surface that
    explicitly rather than defaulting to STABLE.
    """

    type: Literal["lifecycle_stage"] = "lifecycle_stage"
    stage: LifecycleStage


class WorkerModeCompatibility(BaseModel):
    """Declares whether this library is compatible with worker hosting.

    Pair with a ``SuggestedWorkerMode`` to state the author's suggested
    starting point; absence of this declaration is treated as
    ``compatibility=COMPATIBLE``.
    """

    type: Literal["worker_mode_compatibility"] = "worker_mode_compatibility"
    compatibility: WorkerCompatibility


class SuggestedWorkerMode(BaseModel):
    """Declares the author's suggested launch mode (orchestrator vs. worker).

    A starting point, not a hard constraint -- once the GUI override ships,
    users can flip a worker-compatible library between modes. Absence of
    this declaration is treated as "no author suggestion"; consumers apply
    the engine default (today: orchestrator).

    Only meaningful when paired with ``WorkerCompatibility.COMPATIBLE``. A
    ``LibraryMetadata`` validator rejects the contradictory pairing of
    ``INCOMPATIBLE`` with ``mode=WORKER``.
    """

    type: Literal["suggested_worker_mode"] = "suggested_worker_mode"
    mode: WorkerMode


# ---------- Model catalog (provider -> model) ----------


class Model(BaseModel):
    """A single model a provider offers. Leaf of the catalog.

    Identified by its key in the parent provider's `models` dict, not by a
    field on this class -- the key is the stable handle that admin policies and
    node references use. Model keys must be unique across the whole library, so
    a node's `model_usage` can reference one by key alone.

    Multiple entries can describe the same upstream `provider_model_id` with
    different `key_support`; they appear as two dict entries with two keys.

    `family` is an optional UI grouping tag (e.g. "Claude 4", "GPT-4"). It does
    not affect identity or resolution -- it only lets consumers cluster related
    models for display.

    `notes` is free-form author guidance surfaced alongside the model in UIs and
    admin tooling (e.g. "BYOK requires injecting a provider-specific prompt
    driver"). Use it for caveats that don't fit other fields.
    """

    display_name: str
    provider_model_id: str | None = None
    family: str | None = None
    key_support: KeySupport
    terms_url: str | None = None
    notes: str | None = None


class ModelProvider(BaseModel):
    """A model provider (e.g. 'Anthropic', 'OpenAI', 'Kling', 'Ollama').

    `notes` is free-form author guidance applying to every model under the
    provider (e.g. "BYOK requires injecting a provider-specific prompt
    driver"). Per-model `notes` are additive.

    `key_support` is the default for providers that declare no models of their
    own -- e.g. a local-runtime provider like Ollama where
    `key_support=NO_KEY_REQUIRED` is the only meaningful signal. When the
    provider declares models, each model carries its own required `key_support`
    and this provider-level value is unused.
    """

    display_name: str
    terms_url: str | None = None
    notes: str | None = None
    key_support: KeySupport | None = None
    models: dict[str, Model] = Field(default_factory=dict)


class ModelCatalogLibraryProperty(BaseModel):
    """Library-level declaration of available models, organized by provider then model."""

    type: Literal["model_catalog"] = "model_catalog"
    providers: dict[str, ModelProvider] = Field(default_factory=dict)


# `Annotated[X | Y, Field(discriminator="type")]` is Pydantic v2's discriminated-union
# idiom. Breakdown:
#   - `X | Y` is the union of valid member classes.
#   - `typing.Annotated[T, extra]` attaches metadata to a type without changing it at
#     runtime; Pydantic reads that metadata to know how to validate and serialize the type.
#   - `Field(discriminator="type")` tells Pydantic: "look at the `type` attribute of each
#     incoming dict to decide which class to build." Each union member must have a
#     `type: Literal[...]` attribute with a distinct string. Unknown `type` values raise
#     ValidationError (strict validation).
# This is the canonical Pydantic v2 way to round-trip "one of several shapes" through JSON.
LibraryDeclaration = Annotated[
    LifecycleStageLibraryProperty | WorkerModeCompatibility | SuggestedWorkerMode | ModelCatalogLibraryProperty,
    Field(discriminator="type"),
]


def requires_worker_process(declarations: Sequence[LibraryDeclaration]) -> bool:
    """Resolve the load-time worker-process decision from a library's declarations.

    A library requires a dedicated worker subprocess when:

    1. It is compatible with worker hosting (``WorkerCompatibility.COMPATIBLE``
       -- absence of a ``WorkerModeCompatibility`` is treated as ``COMPATIBLE``),
       AND
    2. Its suggested launch mode is ``WorkerMode.WORKER``.

    Anything else -- ``INCOMPATIBLE`` capability, no
    ``SuggestedWorkerMode`` at all, or ``ORCHESTRATOR`` suggestion --
    means the library runs in the orchestrator process.

    Centralized here so the future GUI flip updates only one site.
    """
    capability = next((d for d in declarations if isinstance(d, WorkerModeCompatibility)), None)
    if capability is not None and capability.compatibility is WorkerCompatibility.INCOMPATIBLE:
        return False
    suggested = next((d for d in declarations if isinstance(d, SuggestedWorkerMode)), None)
    if suggested is None:
        return False
    return suggested.mode is WorkerMode.WORKER


class ResolvedModel(NamedTuple):
    """A model paired with its parent provider's identifier and object."""

    provider_id: str
    model_id: str
    model: Model
    provider: ModelProvider


def iter_catalog_models(catalog: ModelCatalogLibraryProperty) -> Iterator[ResolvedModel]:
    """Yield every model in the catalog with its parent provider context.

    Order: provider insertion order, then model insertion order within each
    provider.
    """
    for provider_id, provider in catalog.providers.items():
        for model_id, model in provider.models.items():
            yield ResolvedModel(
                provider_id=provider_id,
                model_id=model_id,
                model=model,
                provider=provider,
            )


# ---------- Node-level declarations ----------


class LifecycleStageNodeProperty(BaseModel):
    """Lifecycle stage override for an individual node.

    Absence of this property on a node means "inherit from the library." Presence
    overrides with the declared value. Consumers (UI) resolve inheritance at display
    time; the schema layer stores only what the author wrote.
    """

    type: Literal["lifecycle_stage"] = "lifecycle_stage"
    stage: LifecycleStage


class ModelUsageNodeProperty(BaseModel):
    """References specific catalog models the node uses, by their catalog dict keys.

    Each entry must resolve to a model somewhere in the library's
    `ModelCatalogLibraryProperty` (validated at library load).

    Use this when the node binds to a specific, named set of models. For nodes
    that dynamically enumerate everything a provider offers at runtime, see
    `ModelProviderUsageNodeProperty`.
    """

    type: Literal["model_usage"] = "model_usage"
    model_ids: list[str]


class ModelProviderUsageNodeProperty(BaseModel):
    """References whole providers the node uses.

    Use this when a node dynamically enumerates every model across an entire
    provider at runtime. Each entry must resolve to a provider declared in the
    library's `ModelCatalogLibraryProperty` (validated at library load).
    """

    type: Literal["model_provider_usage"] = "model_provider_usage"
    provider_ids: list[str]


# See the comment above `LibraryDeclaration` for how `Annotated[... discriminator ...]` works.
NodeDeclaration = Annotated[
    LifecycleStageNodeProperty | ModelUsageNodeProperty | ModelProviderUsageNodeProperty,
    Field(discriminator="type"),
]
