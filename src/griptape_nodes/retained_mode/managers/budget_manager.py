"""BudgetManager - Describes the engine context an outbound call should be attributed to.

The manager owns no state. Every fact is read from a peer manager at request time, so
a project switch between two calls is reflected on the second one.

Nothing here makes a network call, holds a credential, or enforces a budget. It composes
one attribution fact and stops.

**On keeping the header value out of logs and broadcasts.** Three defaults and a
convention protect it; none of them is an enforced invariant, and each has a bypass worth
knowing about:

- `GetAttributionContextRequest.broadcast_result` defaults to False, which keeps the
  Success payload off the WebSocket feed -- but it is a per-instance field any caller can
  set back to True.
- Post-dispatch hooks receive the full result object and run regardless of
  `broadcast_result` or event suppression.
- `result_details` is logged as well as broadcast, so this module never interpolates
  `header_value` or the payload into one. That discipline is held by convention: the
  engine's `omit_from_result` redaction primitive is request-side only and cannot mark a
  result field non-broadcastable.
- On a worker, the request is forwarded to the orchestrator (see
  `app/worker_routing.FORWARDED_REQUEST_TYPES`), so the Success payload crosses the
  worker response topic by construction, whatever `broadcast_result` says.

None of that is alarming on its own -- the value is descriptive, not secret -- but it is
the real state of the mechanism rather than a guarantee.

`Engine.handle_request` already converts a raised handler exception into a
`ResultPayloadFailure`, so this module deliberately has no blanket try/except around the
handler body; the resolvers guard their own peer reads and nothing else can escape.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.budget_events import (
    ATTRIBUTION_SCHEMA_VERSION,
    UNSAVED_WORKFLOW_SENTINEL,
    GetAttributionContextRequest,
    GetAttributionContextResultFailure,
    GetAttributionContextResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

# The Cloud truncates the chain at this depth anyway. Truncating client-side means both
# ends agree and the oversize path is unreachable in practice.
_MAX_PROJECT_CHAIN_ENTRIES = 10

# base64 inflates 4:3, so 4096 decoded bytes encode to at most 5464 -- inside the 5.5 KB
# raw ceiling, which is itself well under nginx's 8 KB default header limit.
_MAX_DECODED_PAYLOAD_BYTES = 4096


class _AttributionFacts(NamedTuple):
    """The engine facts one outbound call is attributed to, as resolved for a single request.

    A value of None means the engine could not determine that fact; the corresponding payload
    key is then omitted, which is how "unknown" is expressed on the wire.
    """

    project_chain: list[str]
    workflow_name: str | None
    node_type: str | None
    engine_id: str | None
    orchestrator_engine_id: str | None
    session_id: str | None


class _AttributionEncoding(NamedTuple):
    """What was actually encoded, so the result's structured fields cannot drift from the header."""

    header_value: str
    facts: _AttributionFacts
    chain_truncated: bool


def _build_attribution_payload(facts: _AttributionFacts) -> dict[str, Any]:
    """Build the decoded attribution payload, omitting every key the engine could not determine.

    An absent key means "unknown". `tags` is omitted entirely when the chain is empty, because an
    empty list would assert "belongs to zero projects" -- a claim we cannot make.
    """
    payload: dict[str, Any] = {"v": ATTRIBUTION_SCHEMA_VERSION}
    if facts.project_chain:
        payload["tags"] = {"project": list(facts.project_chain)}
    if facts.workflow_name is not None:
        payload["workflow"] = facts.workflow_name
    if facts.node_type is not None:
        payload["node_type"] = facts.node_type
    if facts.engine_id is not None:
        payload["engine_id"] = facts.engine_id
    if facts.orchestrator_engine_id is not None:
        payload["orchestrator_engine_id"] = facts.orchestrator_engine_id
    if facts.session_id is not None:
        payload["session_id"] = facts.session_id
    return payload


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url, or None when it exceeds the size cap.

    Padding is kept so the Cloud can call `urlsafe_b64decode` without re-padding.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > _MAX_DECODED_PAYLOAD_BYTES:
        return None
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _encode_attribution_header(facts: _AttributionFacts) -> _AttributionEncoding | None:
    """Encode the attribution header, reducing the payload once if it does not fit.

    The chain is always truncated to `_MAX_PROJECT_CHAIN_ENTRIES` from the leaf: that is the v1
    rule, not a size response. If the result still does not fit, one reduction step drops the
    workflow and node type and keeps only the leaf project. Returns None when even that does not
    fit, which needs a single project id larger than the whole cap.
    """
    chain_truncated = len(facts.project_chain) > _MAX_PROJECT_CHAIN_ENTRIES
    full_facts = facts._replace(project_chain=list(facts.project_chain[:_MAX_PROJECT_CHAIN_ENTRIES]))

    full_header_value = _encode_attribution_payload(_build_attribution_payload(full_facts))
    if full_header_value is not None:
        return _AttributionEncoding(
            header_value=full_header_value,
            facts=full_facts,
            chain_truncated=chain_truncated,
        )

    logger.warning(
        "Attribution payload exceeded %d bytes; reducing it to the leaf project only. "
        "The workflow and node type will not be attributed for this call.",
        _MAX_DECODED_PAYLOAD_BYTES,
    )
    reduced_facts = full_facts._replace(
        project_chain=full_facts.project_chain[:1],
        workflow_name=None,
        node_type=None,
    )
    reduced_header_value = _encode_attribution_payload(_build_attribution_payload(reduced_facts))
    if reduced_header_value is None:
        return None

    return _AttributionEncoding(
        header_value=reduced_header_value,
        facts=reduced_facts,
        chain_truncated=True,
    )


class BudgetManager(EngineScoped):
    """Composes the attribution context for a credit-consuming call.

    Holds no state: the project chain, workflow, engine, and session are read from peer
    managers on every request, so the answer always reflects the engine as it is now.
    """

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        """Initialize the BudgetManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            engine: The owning Engine, used to resolve peer managers.
        """
        super().__init__(engine)
        event_manager.assign_manager_to_request_type(
            GetAttributionContextRequest, self.on_get_attribution_context_request
        )

    def on_get_attribution_context_request(
        self, request: GetAttributionContextRequest
    ) -> GetAttributionContextResultSuccess | GetAttributionContextResultFailure:
        """Describe the current engine context as an encoded attribution header.

        Every resolver degrades to a missing key rather than a failure: the caller is
        about to spend money, and a partial attribution beats none. The one failure is a
        payload that will not fit in a header even after being reduced.
        """
        facts = _AttributionFacts(
            project_chain=self._resolve_project_chain(),
            workflow_name=self._resolve_workflow_name(),
            node_type=request.node_type,
            engine_id=self._resolve_engine_id(),
            orchestrator_engine_id=self._resolve_orchestrator_engine_id(),
            session_id=self._resolve_session_id(),
        )

        encoding = _encode_attribution_header(facts)
        if encoding is None:
            return GetAttributionContextResultFailure(
                result_details=(
                    "Attempted to describe an outbound request for budget attribution. Failed because "
                    "the attribution payload exceeded the maximum header size even after being reduced. "
                    "The request can proceed, but this spend will not be attributed."
                )
            )

        # Every structured field comes from `encoding.facts`, never from `facts`: the reduction
        # step above can drop values, and a consumer reading these must see exactly what the
        # header encodes.
        encoded = encoding.facts
        return GetAttributionContextResultSuccess(
            header_value=encoding.header_value,
            project_chain=list(encoded.project_chain),
            workflow_name=encoded.workflow_name,
            node_type=encoded.node_type,
            engine_id=encoded.engine_id,
            orchestrator_engine_id=encoded.orchestrator_engine_id,
            session_id=encoded.session_id,
            chain_truncated=encoding.chain_truncated,
            result_details=(
                f"Successfully described the attribution context for an outbound request "
                f"({len(encoded.project_chain)} project(s) in the chain)."
            ),
        )

    def _resolve_project_chain(self) -> list[str]:
        """Resolve the current project's ancestry as ids, leaf-first, or [] when unavailable.

        `ProjectChainEntry.name` is dropped here -- the single place the chain is consumed --
        so no user-authored project label can reach the payload by construction.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return []
        return [entry.id for entry in chain]

    def _resolve_workflow_name(self) -> str | None:
        """Resolve the current workflow's registry key, or None when it cannot be determined.

        A workflow that has never been saved is registered under an `unsaved:<uuid4>` key.
        That uuid is fresh every session, so sending it would put a never-repeating value in
        the Cloud's workflow dimension and a per-session identifier in a proxy-logged header.
        Report the sentinel instead: one low-cardinality bucket for scratch spend, still
        distinguishable from an absent key, which means the engine could not tell at all.
        """
        try:
            if not self.engine.context_manager.has_current_workflow():
                return None
            registry_key = self.engine.context_manager.get_current_workflow_name()
        except Exception:
            logger.warning("Could not resolve the current workflow for budget attribution.", exc_info=True)
            return None

        if registry_key.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX):
            return UNSAVED_WORKFLOW_SENTINEL
        return registry_key

    def _resolve_engine_id(self) -> str | None:
        """Resolve this engine's identifier, or None when it cannot be determined."""
        try:
            return self.engine.engine_identity_manager.engine_id
        except Exception:
            logger.warning("Could not resolve the engine id for budget attribution.", exc_info=True)
            return None

    def _resolve_orchestrator_engine_id(self) -> str | None:
        """Resolve the parent engine's id, set at spawn on workers and unset on the orchestrator.

        Reading an environment variable cannot raise, so this one needs no guard. In practice
        the key is always absent from the payload: worker-originated requests are forwarded to
        the orchestrator, which is not a worker and so has no parent to report.
        """
        return os.getenv("GTN_ORCHESTRATOR_ENGINE_ID")

    def _resolve_session_id(self) -> str | None:
        """Resolve the active session id, or None when it cannot be determined."""
        try:
            return self.engine.session_manager.active_session_id
        except Exception:
            logger.warning("Could not resolve the session id for budget attribution.", exc_info=True)
            return None
