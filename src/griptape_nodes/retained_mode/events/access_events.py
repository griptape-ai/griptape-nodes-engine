from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial


@dataclass(frozen=True)
class ModelAccessVerdict:
    """The per-model authorization verdict for one candidate.

    `model_id` is the catalog key the engine evaluated (e.g. ``gtc_claude_opus_4_7``);
    always populated, so it is the field to match a verdict back to a candidate.
    `provider_model_id` is the upstream provider's name for that model (e.g.
    ``claude-opus-4-7``), populated from the library catalog when the candidate
    resolves to a known entry. It is ``None`` both when the candidate did not
    resolve AND when it resolved to an entry that declares no ``provider_model_id``,
    so ``None`` cannot be read as "unresolved" -- match on `model_id` for identity
    and treat `provider_model_id` purely as the upstream display handle.

    `denial` is ``None`` when the model is allowed; a ``CheckpointDenial``
    otherwise, carrying the same failure tuple any other denied checkpoint
    surfaces (so the UI renders identical reason text).
    """

    model_id: str
    provider_model_id: str | None
    denial: CheckpointDenial | None


@dataclass
@PayloadRegistry.register
class QueryModelAccessRequest(RequestPayload):
    """The bare form: 'are these model ids allowed?' No node, no catalog scope.

    Use from non-node callers (sidebar agent, ``ModelManager`` Hugging Face
    enumeration, scripted callers) when there is no engine-side context to
    attribute the query to. The checkpoint carries only ``ID`` (the model id) per
    candidate -- no ``NODE_TYPE``, no ``PROVIDER_ID``, no ``MODEL_FAMILIES``. A
    policy matching on the bare model id still fires.

    Args:
        candidate_model_ids: Catalog keys to evaluate, in input order.

    Results: ``QueryModelAccessResultSuccess`` (one verdict per candidate, in
        input order; ``provider_model_id`` is always ``None`` since no catalog
        is consulted).
    """

    candidate_model_ids: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class QueryModelAccessResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """One verdict per candidate, in input order. Length equals the request's candidate count.

    Args:
        verdicts: Ordered list of per-model verdicts.
    """

    verdicts: list[ModelAccessVerdict] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class QueryModelAccessForNodeRequest(RequestPayload):
    """Node-attributed: 'on behalf of this node, are these ids allowed?'.

    The checkpoint carries ``ID`` (the model id), ``NODE_TYPE = node_type``, and
    (when the id resolves against the node's library catalog) ``PROVIDER_ID`` and
    ``MODEL_FAMILIES``. When ``candidate_model_ids`` is ``None`` the engine
    derives the list from the node's ``ModelUsageNodeProperty`` plus
    ``ModelProviderUsageNodeProperty`` expansion -- the canonical "populate a
    statically declared dropdown" flow.

    Args:
        node_type: The node class name (e.g. ``"DescribeImage"``).
        specific_library_name: Optional disambiguator when multiple libraries
            register the same node type.
        candidate_model_ids: When ``None`` (default), the engine derives
            candidates from the node's declarations. When a list is supplied,
            it overrides the derivation -- useful when the caller has already
            narrowed the set or is querying a subset for badge details.

    Results: ``QueryModelAccessForNodeResultSuccess`` (one verdict per
        candidate, in declaration / input order) |
        ``QueryModelAccessForNodeResultFailure`` (node type or library not
        found).
    """

    node_type: str
    specific_library_name: str | None = None
    candidate_model_ids: list[str] | None = None


@dataclass
@PayloadRegistry.register
class QueryModelAccessForNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """One verdict per candidate, in declaration order (derived) or input order (explicit).

    De-duplication applies only to engine-derived candidates (when
    ``candidate_model_ids`` is omitted); an explicit ``candidate_model_ids`` list
    is evaluated as-is, one verdict per entry including any duplicates.

    Args:
        verdicts: Ordered list of per-model verdicts. Empty when the node
            declares no models and no explicit candidates were supplied.
    """

    verdicts: list[ModelAccessVerdict] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class QueryModelAccessForNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Per-node access query failed. Common cause: node type not registered."""


@dataclass
@PayloadRegistry.register
class QueryModelAccessForCatalogRequest(RequestPayload):
    """Catalog-scoped: caller has a library in mind but no node.

    The checkpoint carries ``ID`` (the model id) and (when the id resolves against
    that library's catalog) ``PROVIDER_ID`` and ``MODEL_FAMILIES``. No ``NODE_TYPE``
    attribute -- the query is not attributed to a node. Distinct from
    ``QueryModelAccessRequest`` because the caller is telling the engine WHICH
    library catalog to use for enrichment.

    A ``library_name`` that does not resolve is not an error -- the
    handler returns ``Success`` with bare ``ID``-only verdicts. Callers
    are expected to know their library; a missing library means the catalog
    has shifted out from under a stale name and a policy can still match on
    the bare ids.

    Args:
        library_name: Library whose catalog supplies provider / family
            enrichment.
        candidate_model_ids: Catalog keys to evaluate, in input order.

    Results: ``QueryModelAccessForCatalogResultSuccess`` (one verdict per
        candidate, in input order).
    """

    library_name: str
    candidate_model_ids: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class QueryModelAccessForCatalogResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """One verdict per candidate, in input order. Length equals the request's candidate count.

    Args:
        verdicts: Ordered list of per-model verdicts.
    """

    verdicts: list[ModelAccessVerdict] = field(default_factory=list)
