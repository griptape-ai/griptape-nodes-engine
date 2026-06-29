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

This manager depends only on the event bus (for the hook chain) and the
`LibraryRegistry` (for catalog resolution). It does not depend on `NodeManager`
or `ModelManager`. Future "can I use this?" queries beyond models (e.g. listing
codecs allowed for the current context) are the natural extension here; the
per-operation runtime checks (e.g. "am I allowed this codec for this file?")
belong with the manager that owns the operation -- same way `INVOKE_MODEL`
fires from `ModelManager`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.library_declarations import (
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
    ResolvedModel,
    find_model_catalog,
    iter_catalog_models,
    resolve_node_models,
)
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForCatalogRequest,
    QueryModelAccessForCatalogResultSuccess,
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
    QueryModelAccessRequest,
    QueryModelAccessResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointSubjectType,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.node_library.library_declarations import ModelCatalogLibraryProperty, NodeDeclaration
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


class AccessManager:
    """Answers per-candidate authorization queries via the `OFFER_MODEL` checkpoint."""

    def __init__(self, event_manager: EventManager | None = None) -> None:
        if event_manager is not None:
            event_manager.assign_manager_to_request_type(QueryModelAccessRequest, self.on_query_model_access_request)
            event_manager.assign_manager_to_request_type(
                QueryModelAccessForNodeRequest, self.on_query_model_access_for_node_request
            )
            event_manager.assign_manager_to_request_type(
                QueryModelAccessForCatalogRequest, self.on_query_model_access_for_catalog_request
            )

    def on_query_model_access_request(self, request: QueryModelAccessRequest) -> ResultPayload:
        """Bare form. Hook sees `ID` (the model id) per candidate; no `NODE_TYPE`, no catalog enrichment."""
        verdicts = self._evaluate(
            candidate_model_ids=request.candidate_model_ids,
            node_type=None,
            resolved_by_id={},
        )
        return QueryModelAccessResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} candidate model(s).",
        )

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
        )
        return QueryModelAccessForNodeResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} model(s) for '{request.node_type}'.",
        )

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
        )
        return QueryModelAccessForCatalogResultSuccess(
            verdicts=verdicts,
            result_details=f"Evaluated {len(verdicts)} candidate model(s) in catalog '{request.library_name}'.",
        )

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
        except KeyError as exc:
            detail = exc.args[0] if exc.args else str(exc)
            msg = (
                f"Attempted to query model access for node type '{node_type}'. "
                f"Failed because the library could not be resolved: {detail}"
            )
            raise _NodeLookupError(msg) from exc

        try:
            node_metadata = library.get_node_metadata(node_type)
        except KeyError as exc:
            library_name = exc.args[0] if exc.args else specific_library_name
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
    def _evaluate(
        *,
        candidate_model_ids: Sequence[str],
        node_type: str | None,
        resolved_by_id: dict[str, ResolvedModel],
    ) -> list[ModelAccessVerdict]:
        """Ask the authorization hook chain once per candidate; collect one verdict each.

        Attribute composition:
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
        event_manager = GriptapeNodes.EventManager()
        verdicts: list[ModelAccessVerdict] = []
        for model_id in candidate_model_ids:
            attributes: dict[str, Any] = {CheckpointAttribute.ID: model_id}
            if node_type is not None:
                attributes[CheckpointAttribute.NODE_TYPE] = node_type
            resolved = resolved_by_id.get(model_id)
            provider_model_id: str | None = None
            if resolved is not None:
                attributes[CheckpointAttribute.PROVIDER_ID] = resolved.provider_id
                if resolved.model.family:
                    attributes[CheckpointAttribute.MODEL_FAMILIES] = [resolved.model.family]
                provider_model_id = resolved.model.provider_model_id
            denial = event_manager.evaluate_authorization_checkpoint(
                AuthorizationCheckpoint(
                    action=CheckpointAction.OFFER_MODEL,
                    subject_type=CheckpointSubjectType.MODEL,
                    subject_id=model_id,
                    attributes=attributes,
                )
            )
            verdicts.append(ModelAccessVerdict(model_id=model_id, provider_model_id=provider_model_id, denial=denial))
        return verdicts
