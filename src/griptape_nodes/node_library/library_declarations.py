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

import re
from collections import defaultdict
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator

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
    """Accepted for backward compatibility; no longer affects where a library runs."""

    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"


class WorkerMode(StrEnum):
    """Accepted for backward compatibility; no longer affects where a library runs.

    Still the type of ``LibraryRegistration.worker_mode_override``.
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


# The editor opens this URL directly in a new browser tab/window, so only http(s) is
# accepted -- a javascript:/data: URL must never reach that call. Shared by the library-
# and node-level properties below.
_DOCUMENTATION_URL_PATTERN = re.compile(r"^https?://")

# The only placeholders `DocumentationLibraryProperty.url_template` may use. Checked by
# attempting the substitution at declaration-parse time, so an author's typo (e.g.
# `{nod}`) fails library load with a clear message instead of resolving silently wrong
# for every node.
_DOCUMENTATION_TEMPLATE_TOKENS = {"category": "", "node": ""}


def _validate_documentation_url(url: str) -> str:
    if not _DOCUMENTATION_URL_PATTERN.match(url):
        msg = f"Documentation url '{url}' is not valid. It must start with 'http://' or 'https://'."
        raise ValueError(msg)
    return url


def _validate_documentation_template(template: str) -> str:
    if not _DOCUMENTATION_URL_PATTERN.match(template):
        msg = f"Documentation url_template '{template}' is not valid. It must start with 'http://' or 'https://'."
        raise ValueError(msg)
    try:
        template.format(**_DOCUMENTATION_TEMPLATE_TOKENS)
    except (KeyError, IndexError) as e:
        msg = (
            f"Documentation url_template '{template}' uses a placeholder other than "
            f"{sorted(_DOCUMENTATION_TEMPLATE_TOKENS)}."
        )
        raise ValueError(msg) from e
    return template


class DocumentationLibraryProperty(BaseModel):
    """A documentation link for the library as a whole.

    Exactly one of `url` (one literal link shared by every node) or `url_template`
    (interpolated per node) must be set. `url_template` exists because a library's
    nodes number in the hundreds; hand-writing a per-node override for each one
    is impractical, so a library with an organized docs site can give every node
    its own page from a single declaration, e.g.
    `"https://docs.example.com/nodes/{category}/{node}/"`. Supported placeholders
    are `{category}` (the node's declared `category`) and `{node}` (its class
    name); both are URL-escaped before substitution.

    Node-level `documentation` (see `DocumentationNodeProperty`) overrides this for
    an individual node with a literal url, the same way `lifecycle_stage` does.
    Consumers resolve that inheritance at display time; this model stores only
    what the author wrote.
    """

    type: Literal["documentation"] = "documentation"
    url: str | None = None
    url_template: str | None = None

    @model_validator(mode="after")
    def _exactly_one_of_url_or_template(self) -> DocumentationLibraryProperty:
        if (self.url is None) == (self.url_template is None):
            msg = "Documentation declaration must set exactly one of 'url' or 'url_template', not both or neither."
            raise ValueError(msg)
        if self.url is not None:
            _validate_documentation_url(self.url)
        elif self.url_template is not None:
            _validate_documentation_template(self.url_template)
        return self


class WorkerModeCompatibility(BaseModel):
    """Accepted for backward compatibility; no longer affects where a library runs.

    A library's nodes execute in a worker when it declares
    ``pip_dependencies_exec``.
    """

    type: Literal["worker_mode_compatibility"] = "worker_mode_compatibility"
    compatibility: WorkerCompatibility


class SuggestedWorkerMode(BaseModel):
    """Accepted for backward compatibility; no longer affects where a library runs.

    A library's nodes execute in a worker when it declares
    ``pip_dependencies_exec``. A manifest declaring ``mode=WORKER`` loads and
    runs in-process, and says so once at load time.
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


class SidebarModelProvider(ModelProvider):
    """Extends ModelProvider with UI fields used by the engine's chat sidebar.

    These fields are engine/sidebar-specific and do not belong in a library's
    griptape_nodes_library.json declaration.
    """

    default_base_url: str | None = None
    has_model_list: bool = False
    default_model: str = ""


class ModelCatalogSidebarProperty(BaseModel):
    """Engine-side catalog that merges library provider metadata with sidebar UI fields."""

    providers: dict[str, SidebarModelProvider] = Field(default_factory=dict)


class LibraryDependencyDeclaration(BaseModel):
    """Declares another Griptape Node Library that this library depends on.

    Placed in ``metadata.declarations`` so consumers can inspect inter-library
    dependencies before committing to install anything.
    """

    type: Literal["library_dependency"] = "library_dependency"
    url: str
    required: bool = True


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
    LifecycleStageLibraryProperty
    | WorkerModeCompatibility
    | SuggestedWorkerMode
    | ModelCatalogLibraryProperty
    | LibraryDependencyDeclaration
    | DocumentationLibraryProperty,
    Field(discriminator="type"),
]


def find_model_catalog(declarations: Sequence[LibraryDeclaration]) -> ModelCatalogLibraryProperty | None:
    """Return the library's `model_catalog` declaration, or `None` when it declares none.

    The single primitive every catalog consumer builds on: callers that want the
    resolved models pair this with `iter_catalog_models` / `resolve_node_models`,
    callers that only need the declaration (e.g. an existence check) use it alone.
    """
    return next((d for d in declarations if isinstance(d, ModelCatalogLibraryProperty)), None)


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


class DocumentationNodeProperty(BaseModel):
    """Documentation link override for an individual node.

    Absence of this property on a node means "inherit the library's documentation
    link," the same way `LifecycleStageNodeProperty` inherits `lifecycle_stage`.
    Presence overrides with the declared value. Consumers (UI) resolve inheritance
    at display time; the schema layer stores only what the author wrote.
    """

    type: Literal["documentation"] = "documentation"
    url: str

    @field_validator("url")
    @classmethod
    def _url_is_http_or_https(cls, v: str) -> str:
        return _validate_documentation_url(v)


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


class ArbitraryPythonExecutionNodeProperty(BaseModel):
    """Declares that this node executes arbitrary Python code supplied at runtime.

    A security-relevant identity fact: a node carrying this property runs Python
    code that is not vetted at authoring time (e.g. an artist-supplied script).
    Consumers (UI) can surface a warning before such a node runs. Absence of this
    declaration means the node does not execute arbitrary Python.
    """

    type: Literal["arbitrary_python_execution"] = "arbitrary_python_execution"
    executes_arbitrary_python: bool


# See the comment above `LibraryDeclaration` for how `Annotated[... discriminator ...]` works.
NodeDeclaration = Annotated[
    LifecycleStageNodeProperty
    | ModelUsageNodeProperty
    | ModelProviderUsageNodeProperty
    | ArbitraryPythonExecutionNodeProperty
    | DocumentationNodeProperty,
    Field(discriminator="type"),
]


def resolve_node_documentation_url(
    node_declarations: Sequence[NodeDeclaration],
    library_declarations: Sequence[LibraryDeclaration],
    *,
    node_type: str,
    node_category: str,
) -> str | None:
    """Resolve a node's effective documentation url: its own override, else the library's.

    Mirrors how `lifecycle_stage` inherits from the library when a node declares no
    override of its own (see `LifecycleStageNodeProperty`). A node-level override is
    always a literal `url`. A library-level declaration contributes either its literal
    `url` or its `url_template` interpolated with this node's `node_category` and
    `node_type` (URL-escaped, so a category/class name with reserved characters can't
    break out of the templated path segment). Returns `None` when neither the node nor
    the library declares `documentation`.
    """
    node_url = next(
        (declaration.url for declaration in node_declarations if isinstance(declaration, DocumentationNodeProperty)),
        None,
    )
    if node_url is not None:
        return node_url
    library_declaration = next(
        (declaration for declaration in library_declarations if isinstance(declaration, DocumentationLibraryProperty)),
        None,
    )
    if library_declaration is None:
        return None
    if library_declaration.url is not None:
        return library_declaration.url
    # `_exactly_one_of_url_or_template` guarantees url_template is set whenever url is not.
    url_template = library_declaration.url_template
    if url_template is None:
        return None
    return url_template.format(category=quote(node_category, safe=""), node=quote(node_type, safe=""))


def _iter_referenced_models(
    declaration: NodeDeclaration,
    models_by_id: dict[str, ResolvedModel],
    models_by_provider: dict[str, list[ResolvedModel]],
) -> Iterator[ResolvedModel]:
    """Yield the catalog models a single node declaration references.

    Unresolved ids/providers yield nothing; the caller relies on library-load
    validation to surface those as blocking problems.
    """
    if isinstance(declaration, ModelUsageNodeProperty):
        for model_id in declaration.model_ids:
            resolved = models_by_id.get(model_id)
            if resolved is not None:
                yield resolved
    elif isinstance(declaration, ModelProviderUsageNodeProperty):
        for provider_id in declaration.provider_ids:
            yield from models_by_provider.get(provider_id, [])


def resolve_node_models(
    catalog: ModelCatalogLibraryProperty,
    declarations: Sequence[NodeDeclaration],
) -> list[ResolvedModel]:
    """Resolve a node's model declarations against a catalog into concrete models.

    Walks the node's ``model_usage`` (specific model ids) and
    ``model_provider_usage`` (whole providers) declarations and returns the
    matching models with their provider context. Results follow declaration
    order, then catalog order within a ``model_provider_usage`` entry, and are
    de-duplicated by ``(provider_id, model_id)`` so a model named both directly
    and via its provider appears once.

    References that do not resolve are skipped rather than raised: library load
    already blocks unresolved references via ``validate_library_declarations``,
    so a miss at runtime means the catalog shifted under a stale reference and
    the node simply offers fewer models.
    """
    models_by_id: dict[str, ResolvedModel] = {}
    models_by_provider: dict[str, list[ResolvedModel]] = defaultdict(list)
    for resolved in iter_catalog_models(catalog):
        models_by_id[resolved.model_id] = resolved
        models_by_provider[resolved.provider_id].append(resolved)

    ordered: list[ResolvedModel] = []
    seen: set[tuple[str, str]] = set()
    for declaration in declarations:
        for resolved in _iter_referenced_models(declaration, models_by_id, models_by_provider):
            key = (resolved.provider_id, resolved.model_id)
            if key not in seen:
                seen.add(key)
                ordered.append(resolved)
    return ordered
