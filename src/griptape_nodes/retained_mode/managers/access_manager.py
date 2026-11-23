"""Per-candidate authorization queries: the `OFFER_MODEL` checkpoint, asked once per id.

The node-instantiation checkpoint (`INSTANTIATE_NODE`) is a binary gate over the
*union* of a node's declared models -- correct for a node dedicated to one model,
but useless for nodes that offer a dropdown of many. This manager is the engine's
answer to "of these N candidates, which are allowed under the current policy, and
why?" -- one verdict per candidate, each carrying a `CheckpointDenial` or `None`.

Three request types, named by what attribution the engine adds to each checkpoint:

  - `QueryModelAccessRequest` -- bare. Caller supplies ids only. Checkpoint
    attributes carry only `ID` (the model id). For non-node callers (sidebar,
    scripted enumerations) where no engine-side context attributes the query.

  - `QueryModelAccessForNodeRequest` -- node-attributed. Caller supplies a node
    type and, optionally, an explicit candidate list. When candidates are
    omitted the engine derives them from the node's `ModelUsageNodeProperty`
    plus `ModelProviderUsageNodeProperty` expansion. Checkpoint attributes
    carry `ID` (the model id), `NODE_TYPE = node_type`, plus catalog-resolved
    `PROVIDER_ID` and `MODEL_FAMILIES` when the id is in the node's library catalog.

  - `QueryModelAccessForCatalogRequest` -- catalog-scoped. Caller supplies a
    library name and ids. Checkpoint attributes carry `ID` (the model id) plus
    catalog-resolved `PROVIDER_ID` / `MODEL_FAMILIES`. No `NODE_TYPE` attribute --
    the caller has a library in mind but no node to attribute the query to.

All three paths share `_evaluate`, which iterates candidates and asks the
authorization hook chain once per id. The chain's recursion guard
(`EventManager._hook_evaluation.authorizing`) is reset after each evaluation, so
N sequential calls from one handler produce N independent verdicts.

`resolve_chat_model` and `chat_model_denial` do the same for the chat sidebar,
called in-process by `AgentManager` rather than through an event.

This manager depends on the event bus (for the hook chain), the `LibraryRegistry`
(for library catalog resolution), and the engine's own model catalog in
`drivers/cloud_models.py` (for chat resolution). It does not depend on
`NodeManager` or `ModelManager`. Future "can I use this?" queries beyond models
(e.g. listing codecs allowed for the current context) are the natural extension
here; the per-operation runtime checks (e.g. "am I allowed this codec for this
file?") belong with the manager that owns the operation -- same way
`INVOKE_MODEL` fires from `ModelManager`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.drivers.cloud_models import (
    CUSTOM_ENDPOINT_PROVIDER_ID,
    DEPRECATED_MODELS,
    ENGINE_MODEL_CATALOG,
    IMAGE_DEPRECATED_MODELS,
    ProviderID,
)
from griptape_nodes.node_library.library_declarations import (
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
    ResolvedModel,
    find_model_catalog,
    iter_catalog_models,
    resolve_node_models,
)
from griptape_nodes.node_library.library_registry import LibraryRegistry, LibraryRegistryError
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.access_events import (
    CodecAccessDirection,
    CodecAccessVerdict,
    ModelAccessVerdict,
    QueryCodecAccessRequest,
    QueryCodecAccessResultFailure,
    QueryCodecAccessResultSuccess,
    QueryModelAccessForCatalogRequest,
    QueryModelAccessForCatalogResultSuccess,
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
    QueryModelAccessRequest,
    QueryModelAccessResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.node_library.library_declarations import ModelCatalogLibraryProperty, NodeDeclaration
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


class _NodeLookupError(Exception):
    """A node's library or metadata could not be resolved for a model-access query.

    Carries an already-formatted sentence (free of `KeyError`'s quote/tuple
    `repr`) that the per-node handler surfaces verbatim on a `Failure` result.
    """


@dataclass(frozen=True, kw_only=True, slots=True)
class _NodeResolution:
    """A node's library context: its declarations, its library's model catalog, and a model-id index.

    Built only on the success path of `AccessManager._resolve_node_library`; a
    failed lookup raises `_NodeLookupError` instead of producing a half-populated
    instance.
    """

    node_declarations: Sequence[NodeDeclaration]
    catalog: ModelCatalogLibraryProperty | None
    resolved_by_id: dict[str, ResolvedModel]


def build_derived_model_id(provider_id: str, model_name: str) -> str:
    """Join a provider and model into the `<provider>/<model>` id a local or custom model gates on.

    `provider_id` must not itself contain `/`, so the join stays unambiguous: a
    provider id with one would make it impossible to tell where the provider
    ends and the model begins.
    """
    if "/" in provider_id:
        msg = f"Attempted to build a derived model id. Failed because provider id '{provider_id}' contains '/'."
        raise ValueError(msg)
    return f"{provider_id}/{model_name}"


# Indexes ENGINE_MODEL_CATALOG's Griptape Cloud models by the wire name the sidebar sends.
_ENGINE_CATALOG_BY_PROVIDER_MODEL_ID: dict[str, ResolvedModel] = {
    resolved.model.provider_model_id: resolved
    for resolved in iter_catalog_models(ENGINE_MODEL_CATALOG)
    if resolved.model.provider_model_id is not None
}


@dataclass(frozen=True, kw_only=True, slots=True)
class ChatModelResolution:
    """A chat/image model's resolved checkpoint identity: its id, provider, and family.

    `model_id` is the engine catalog key for a Griptape Cloud model, or a derived
    `<provider>/<model>` id for everything else. `provider_id` is the API serving
    the call -- the model's vendor for Griptape Cloud, the connection itself for
    local and custom endpoints. `model_families` is always `None` for a derived
    id: family is catalog metadata, and a derived id resolves to no catalog entry.
    """

    model_id: str
    provider_id: str | None
    model_families: list[str] | None


class AccessManager(EngineScoped):
    """Answers per-candidate authorization queries via the `OFFER_MODEL` checkpoint."""

    def __init__(self, event_manager: EventManager | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        if event_manager is not None:
            event_manager.register_request_handlers(self)

    @handles(QueryModelAccessRequest)
    def on_query_model_access_request(self, request: QueryModelAccessRequest) -> ResultPayload:
        """Bare form. Hook sees `ID` (the model id) per candidate; no `NODE_TYPE`, no catalog enrichment."""
        verdicts = self._evaluate(
            candidate_model_ids=request.candidate_model_ids,
            node_type=None,
            resolved_by_id={},
            event_manager=self.engine.event_manager,
        )
        return QueryModelAccessResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} candidate model(s).",
        )

    @handles(QueryModelAccessForNodeRequest)
    def on_query_model_access_for_node_request(self, request: QueryModelAccessForNodeRequest) -> ResultPayload:
        """Node-attributed. Engine derives candidates from declarations unless caller overrides."""
        try:
            resolution = self._resolve_node_library(request.node_type, request.specific_library_name)
        except _NodeLookupError as exc:
            return QueryModelAccessForNodeResultFailure(result_details=str(exc))

        if request.candidate_model_ids is None:
            candidates = self._declared_model_ids(
                node_declarations=resolution.node_declarations,
                catalog=resolution.catalog,
            )
        else:
            candidates = list(request.candidate_model_ids)

        verdicts = self._evaluate(
            candidate_model_ids=candidates,
            node_type=request.node_type,
            resolved_by_id=resolution.resolved_by_id,
            event_manager=self.engine.event_manager,
        )
        return QueryModelAccessForNodeResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} model(s) for '{request.node_type}'.",
        )

    @handles(QueryCodecAccessRequest)
    def on_query_codec_access_request(self, request: QueryCodecAccessRequest) -> ResultPayload:
        """Per-codec offer query. Hook sees `ID` (the codec) plus optional `CONTAINER_FORMAT`.

        Mirrors ``QueryModelAccessRequest`` for codec dropdowns: filter what the
        UI offers against the same policy that gates the actual read or write.
        """
        match request.direction:
            case CodecAccessDirection.READ:
                action = CheckpointAction.READ_VIDEO_CODEC
            case CodecAccessDirection.WRITE:
                action = CheckpointAction.WRITE_VIDEO_CODEC
            case _:
                # Defense in depth for stray wire-side values. StrEnum catches
                # Python-side typos; a request deserialized from the WS boundary
                # can still carry any string.
                valid = ", ".join(f"'{d.value}'" for d in CodecAccessDirection)
                msg = (
                    f"Attempted to query codec access. Failed because direction "
                    f"'{request.direction}' is not one of {valid}."
                )
                return QueryCodecAccessResultFailure(result_details=msg)

        event_manager = self.engine.event_manager
        verdicts: list[CodecAccessVerdict] = []
        for codec in request.candidate_codecs:
            attributes: dict[str, Any] = {CheckpointAttribute.ID: codec}
            if request.container_format is not None:
                attributes[CheckpointAttribute.CONTAINER_FORMAT] = request.container_format
            denial = event_manager.evaluate_authorization_checkpoint(
                AuthorizationCheckpoint(
                    action=action,
                    subject_type=CheckpointSubjectType.VIDEO_CODEC,
                    subject_id=codec,
                    attributes=attributes,
                )
            )
            verdicts.append(CodecAccessVerdict(codec=codec, container_format=request.container_format, denial=denial))
        return QueryCodecAccessResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} candidate codec(s) for '{request.direction}'.",
        )

    @handles(QueryModelAccessForCatalogRequest)
    def on_query_model_access_for_catalog_request(self, request: QueryModelAccessForCatalogRequest) -> ResultPayload:
        """Catalog-scoped. Hook sees `ID` (the model id) plus enrichment when the id resolves.

        A missing library is not fatal -- callers (sidebar, scripts) may name a
        library that's not currently registered; the handler falls through to
        bare verdicts so a policy can still match on `ID`.
        """
        try:
            library = LibraryRegistry.get_library(request.library_name)
        except KeyError:
            resolved_by_id: dict[str, ResolvedModel] = {}
        else:
            resolved_by_id = self._index_catalog(find_model_catalog(library.get_metadata().declarations))

        verdicts = self._evaluate(
            candidate_model_ids=request.candidate_model_ids,
            node_type=None,
            resolved_by_id=resolved_by_id,
            event_manager=self.engine.event_manager,
        )
        return QueryModelAccessForCatalogResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} candidate model(s) in catalog '{request.library_name}'.",
        )

    def chat_model_denial(self, *, action: CheckpointAction, connection_id: str, model_name: str) -> str | None:
        """One formatted refusal for a chat/image model, or `None` when it's permitted.

        `AgentManager` calls this in-process (not through an event), passing
        `OFFER_MODEL` for a sidebar listing or a provider create/update/configure,
        and `INVOKE_MODEL` for the gate that matters: before `_build_runner`
        builds or reuses a runner, and before each image-generation call --
        because `agent.providers` can be edited in the config file directly,
        bypassing every offer check. See `resolve_chat_model` for how
        `connection_id` and `model_name` resolve to a checkpoint's id, provider,
        and family.
        """
        resolved = self.resolve_chat_model(connection_id=connection_id, model_name=model_name)
        if resolved is None:
            return f"'{model_name}' is not a Griptape Cloud catalog model."
        attributes = self._checkpoint_attributes(
            resolved.model_id,
            node_type=None,
            provider_id=resolved.provider_id,
            model_families=resolved.model_families,
        )
        denial = self.engine.event_manager.evaluate_authorization_checkpoint(
            AuthorizationCheckpoint(
                action=action,
                subject_type=CheckpointSubjectType.MODEL,
                subject_id=resolved.model_id,
                attributes=attributes,
            )
        )
        if denial is None:
            return None
        return f"model '{model_name}' is not permitted: {denial.reason()}"

    def _resolve_node_library(self, node_type: str, specific_library_name: str | None) -> _NodeResolution:
        """Look up the node's library and return its declarations plus a model-id index.

        Raises:
            _NodeLookupError: when the node type's library is not registered, or
                the node type is not found within that library. Its message is a
                clean sentence the per-node handler surfaces verbatim on a
                ``Failure`` result.
        """
        try:
            library = LibraryRegistry.get_library_for_node_type(node_type, specific_library_name)
        except LibraryRegistryError as exc:
            msg = (
                f"Attempted to query model access for node type '{node_type}'. "
                f"Failed because the library could not be resolved: {exc}"
            )
            raise _NodeLookupError(msg) from exc

        try:
            node_metadata = library.get_node_metadata(node_type)
        except LibraryRegistryError as exc:
            library_name = library.get_library_data().name
            msg = (
                f"Attempted to query model access for node type '{node_type}'. "
                f"Failed because it is not registered in library '{library_name}'."
            )
            raise _NodeLookupError(msg) from exc

        catalog = find_model_catalog(library.get_metadata().declarations)
        return _NodeResolution(
            node_declarations=node_metadata.declarations,
            catalog=catalog,
            resolved_by_id=self._index_catalog(catalog),
        )

    @staticmethod
    def _index_catalog(catalog: ModelCatalogLibraryProperty | None) -> dict[str, ResolvedModel]:
        """Build a `{model_id: ResolvedModel}` index from a catalog, empty when there is none."""
        if catalog is None:
            return {}
        return {resolved.model_id: resolved for resolved in iter_catalog_models(catalog)}

    @staticmethod
    def _declared_model_ids(
        *,
        node_declarations: Sequence[NodeDeclaration],
        catalog: ModelCatalogLibraryProperty | None,
    ) -> list[str]:
        """Union of catalog ids from `model_usage` plus `model_provider_usage` expansion.

        Order: each ``ModelUsageNodeProperty.model_ids`` entry in declaration order,
        then provider-expanded ids in catalog order, de-duplicated. A node that
        declares no model usage returns an empty list (zero verdicts).

        This deliberately does NOT reuse ``resolve_node_models``: that helper drops
        ``model_usage`` ids absent from the catalog, but a per-candidate access
        query must keep them so a policy can explicitly deny an unknown id rather
        than have it silently vanish from the offered set. Only the provider
        expansion (which is meaningless without a catalog) goes through the
        resolver.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for declaration in node_declarations:
            if isinstance(declaration, ModelUsageNodeProperty):
                for model_id in declaration.model_ids:
                    if model_id not in seen:
                        seen.add(model_id)
                        ordered.append(model_id)

        if catalog is None:
            return ordered

        provider_declarations = [d for d in node_declarations if isinstance(d, ModelProviderUsageNodeProperty)]
        if not provider_declarations:
            return ordered

        for resolved in resolve_node_models(catalog, provider_declarations):
            if resolved.model_id not in seen:
                seen.add(resolved.model_id)
                ordered.append(resolved.model_id)
        return ordered

    @staticmethod
    def resolve_chat_model(*, connection_id: str, model_name: str) -> ChatModelResolution | None:
        """Resolve one sidebar model name to the id/provider/family an `OfferModel` checkpoint gates on.

        `connection_id` is the sidebar's connection type (`griptape_cloud`,
        `ollama`, `lmstudio`, `custom`). A Griptape Cloud model name resolves
        through the engine catalog's `provider_model_id` index to its vendor
        provider and family -- the same catalog key a node's own dropdown would
        resolve to, so a rule written for nodes also gates the same model in
        chat. A name retired from the catalog but still saved in a provider's
        config resolves through `DEPRECATED_MODELS` / `IMAGE_DEPRECATED_MODELS`
        to its replacement's entry instead: only the policy identity changes,
        never the name actually sent. Every other connection has no fixed
        catalog entries, so its model gets a derived id under its own provider
        (`custom` maps to `custom_endpoint`), with no family.

        Returns:
            `None` when `connection_id` is `griptape_cloud` and `model_name` is
                in neither the engine catalog nor the deprecation maps. There is
                no bare-id fallback for this provider: an unresolved id would
                carry no provider or family and so escape every provider- and
                family-scoped deny rule.
        """
        if connection_id != ProviderID.GRIPTAPE_CLOUD:
            provider_id = CUSTOM_ENDPOINT_PROVIDER_ID if connection_id == ProviderID.CUSTOM else connection_id
            return ChatModelResolution(
                model_id=build_derived_model_id(provider_id, model_name),
                provider_id=provider_id,
                model_families=None,
            )
        resolved = _ENGINE_CATALOG_BY_PROVIDER_MODEL_ID.get(model_name)
        if resolved is None:
            replacement_name = DEPRECATED_MODELS.get(model_name) or IMAGE_DEPRECATED_MODELS.get(model_name)
            if replacement_name is not None:
                resolved = _ENGINE_CATALOG_BY_PROVIDER_MODEL_ID.get(replacement_name)
        if resolved is None:
            return None
        families = [resolved.model.family] if resolved.model.family else None
        return ChatModelResolution(
            model_id=resolved.model_id, provider_id=resolved.provider_id, model_families=families
        )

    @staticmethod
    def _checkpoint_attributes(
        model_id: str,
        *,
        node_type: str | None,
        provider_id: str | None,
        model_families: list[str] | None,
    ) -> dict[str, Any]:
        """Build the `id` / `node_type` / `provider_id` / `model_families` a model checkpoint carries.

        Shared by every caller that resolves a model to an `OfferModel` ask --
        node dropdowns, catalog-scoped queries, and the chat sidebar -- so a
        provider- or family-scoped rule gates all of them identically.
        """
        attributes: dict[str, Any] = {CheckpointAttribute.ID: model_id}
        if node_type is not None:
            attributes[CheckpointAttribute.NODE_TYPE] = node_type
        if provider_id is not None:
            attributes[CheckpointAttribute.PROVIDER_ID] = provider_id
        if model_families:
            attributes[CheckpointAttribute.MODEL_FAMILIES] = model_families
        return attributes

    @staticmethod
    def _evaluate(
        *,
        candidate_model_ids: Sequence[str],
        node_type: str | None,
        resolved_by_id: dict[str, ResolvedModel],
        event_manager: EventManager,
    ) -> list[ModelAccessVerdict]:
        """Ask the authorization hook chain once per candidate; collect one verdict each.

        Attribute composition (via `_checkpoint_attributes`):
          - ``ID`` (the model id, mirroring ``subject_id``) always.
          - ``NODE_TYPE = node_type`` only when ``node_type`` is supplied (per-node form).
          - ``PROVIDER_ID`` and ``MODEL_FAMILIES`` only when the id resolves in
            ``resolved_by_id`` (the per-node and catalog-scoped forms supply this;
            the bare form does not).

        An unknown id is still asked of the hook with ``ID`` only, so a
        policy can still match on the bare key.

        Sequential calls reset the hook chain's recursion guard between
        iterations; the guard only short-circuits when a hook itself re-enters a
        guarded engine operation, not when one handler loops over candidates.
        """
        verdicts: list[ModelAccessVerdict] = []
        for model_id in candidate_model_ids:
            resolved = resolved_by_id.get(model_id)
            provider_id: str | None = None
            model_families: list[str] | None = None
            provider_model_id: str | None = None
            display_name: str | None = None
            if resolved is not None:
                provider_id = resolved.provider_id
                if resolved.model.family:
                    model_families = [resolved.model.family]
                provider_model_id = resolved.model.provider_model_id
                display_name = resolved.model.display_name
            attributes = AccessManager._checkpoint_attributes(
                model_id, node_type=node_type, provider_id=provider_id, model_families=model_families
            )
            denial = event_manager.evaluate_authorization_checkpoint(
                AuthorizationCheckpoint(
                    action=CheckpointAction.OFFER_MODEL,
                    subject_type=CheckpointSubjectType.MODEL,
                    subject_id=model_id,
                    attributes=attributes,
                )
            )
            verdicts.append(
                ModelAccessVerdict(
                    model_id=model_id,
                    provider_model_id=provider_model_id,
                    denial=denial,
                    display_name=display_name,
                )
            )
        return verdicts
