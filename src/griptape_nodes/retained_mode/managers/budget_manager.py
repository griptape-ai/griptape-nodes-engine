"""BudgetManager - Describes the engine context an outbound call should be attributed to.

The manager owns no state. Every fact is read from a peer manager at request time, so a
project switch between two calls is reflected on the second one. Nothing here makes a
network call, holds a credential, or enforces a budget.

**Keeping the header value out of logs and broadcasts is a convention, not an invariant.**
`broadcast_result` defaults to False but is a per-instance field any caller can flip;
post-dispatch hooks receive the full result regardless of it; a forwarded worker request
(`app/worker_routing.FORWARDED_REQUEST_TYPES`) puts the Success payload on the worker
response topic by construction; and the engine's `omit_from_result` redaction primitive is
request-side only, so it cannot mark a result field non-broadcastable. What this module can
do it does: it never interpolates `header_value` or the payload into `result_details`, which
is logged as well as broadcast. The value is descriptive rather than secret, so none of that
is alarming -- but it is the real state of the mechanism.

`Engine.handle_request` converts a raised handler exception into a `ResultPayloadFailure`,
so there is deliberately no blanket try/except here; the resolvers guard their own peer reads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
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
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

# Matches the Cloud parser's MAX_CHAIN_LENGTH. Truncating lower would not be conservative: the
# parser flags a truncated chain as mangled because budget paths are root-anchored, so dropping
# ancestors client-side costs matches the Cloud would have made.
_MAX_PROJECT_CHAIN_ENTRIES = 32

# base64 inflates 4:3, so 4096 decoded bytes encode to at most 5464 -- inside the 5.5 KB raw
# ceiling, itself well under nginx's 8 KB default.
_MAX_DECODED_PAYLOAD_BYTES = 4096

# C0 and C1 control characters, mirroring the Cloud parser's storability rule. A value
# containing one is stored nowhere on the far end, so sending it buys nothing and costs
# something -- see `_is_transmissible`.
_UNSTORABLE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class _AttributionFacts(NamedTuple):
    """The engine facts one outbound call is attributed to.

    None means the engine could not determine that fact; its payload key is then omitted,
    which is how "unknown" is expressed on the wire.
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


class _ReductionStage(NamedTuple):
    """One rung of the size-reduction ladder, ordered most informative first."""

    facts: _AttributionFacts
    # None on the unreduced rung; otherwise what this rung gives up, for the warning log.
    reduction_note: str | None


def _is_transmissible(value: str) -> bool:
    """Whether a value survives the trip to the Cloud intact.

    Two ways it does not. A project name is free text and a workflow registry key is derived
    from a filesystem path, so either can carry a byte the wire cannot hold. A path whose bytes
    are not valid UTF-8 comes back from the OS carrying lone surrogates from `surrogateescape`,
    which `str.encode` refuses. A pasted line break, or a directory name that legally contains
    a control character, encodes here and is then discarded by the Cloud's storability check.
    """
    if _UNSTORABLE_CHARACTERS.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _transmissible_or_none(value: str | None) -> str | None:
    """Pass a value through, or None when it cannot reach the Cloud intact.

    Omitting the key says "the engine could not determine this", which is honest: a value
    the far end would discard is one the engine cannot express.
    """
    if value is None:
        return None
    if not _is_transmissible(value):
        return None
    return value


def _build_attribution_payload(facts: _AttributionFacts) -> dict[str, Any]:
    """Build the decoded payload, omitting every key the engine could not determine.

    Every dimension lives under `tags`; the Cloud parser reads nothing else, and there is no
    second namespace beside it. `tags` itself is omitted when the engine could determine
    nothing, rather than sent empty.
    """
    tags: dict[str, Any] = {}
    if facts.project_chain:
        tags["project"] = list(facts.project_chain)
    if facts.workflow_name is not None:
        tags["workflow"] = facts.workflow_name
    if facts.node_type is not None:
        tags["node_type"] = facts.node_type
    if facts.engine_id is not None:
        tags["engine_id"] = facts.engine_id
    if facts.orchestrator_engine_id is not None:
        tags["orchestrator_engine_id"] = facts.orchestrator_engine_id
    if facts.session_id is not None:
        tags["session_id"] = facts.session_id

    payload: dict[str, Any] = {"v": ATTRIBUTION_SCHEMA_VERSION}
    if tags:
        payload["tags"] = tags
    return payload


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url, or None when it cannot be encoded or exceeds the cap.

    Padding is kept so the Cloud can call `urlsafe_b64decode` without re-padding.

    The UTF-8 guard is a backstop -- every dimension is filtered through `_transmissible_or_none`
    upstream, which costs one key where returning None here costs the whole header. It stays
    because letting `str.encode` raise would escape the handler as a `GenericResultFailure`,
    which ignores `failure_log_level` (`event_manager.py:1088-1094`) and would log an ERROR with
    a traceback on every metered call.
    """
    try:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        logger.warning(
            "Attribution payload could not be encoded as UTF-8; sending no attribution header.",
            exc_info=True,
        )
        return None
    if len(raw) > _MAX_DECODED_PAYLOAD_BYTES:
        return None
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _encode_attribution_header(facts: _AttributionFacts) -> _AttributionEncoding | None:
    """Encode the attribution header, shedding dimensions in stages until it fits.

    The chain is always truncated to `_MAX_PROJECT_CHAIN_ENTRIES` from the leaf -- a standing
    rule, not a size response. If the result does not fit, dimensions are shed in order of increasing
    value: the workflow and node type first, and only then the chain's ancestors.

    The chain goes last because it is the only dimension the Cloud matches budgets against.
    Shedding it first would trade the billable dimension for audit-only labels, even when an
    oversized `workflow` was what pushed the payload over.

    When it does go, all of it goes. Budget paths are root-anchored, so keeping the leaf buys
    no narrower match; it presents a nested project as a root, which matches nothing at best
    and bills an unrelated top-level project of the same name at worst. Same rule
    `_resolve_project_chain` applies to a name it cannot describe.

    None means the payload did not fit even with no chain and no labels, leaving only the
    schema version and a few guids -- unreachable short of a cap under a hundred bytes.
    """
    chain_truncated = len(facts.project_chain) > _MAX_PROJECT_CHAIN_ENTRIES
    full_facts = facts._replace(project_chain=list(facts.project_chain[:_MAX_PROJECT_CHAIN_ENTRIES]))
    without_labels = full_facts._replace(workflow_name=None, node_type=None)
    no_chain = without_labels._replace(project_chain=[])

    stages = (
        _ReductionStage(facts=full_facts, reduction_note=None),
        _ReductionStage(
            facts=without_labels,
            reduction_note="the workflow and node type will not be attributed for this call",
        ),
        _ReductionStage(
            facts=no_chain,
            reduction_note="no project will be attributed for this call",
        ),
    )

    for stage in stages:
        header_value = _encode_attribution_payload(_build_attribution_payload(stage.facts))
        if header_value is None:
            continue
        if stage.reduction_note is not None:
            logger.warning(
                "Attribution payload exceeded %d bytes; reducing it so %s.",
                _MAX_DECODED_PAYLOAD_BYTES,
                stage.reduction_note,
            )
        return _AttributionEncoding(
            header_value=header_value,
            facts=stage.facts,
            # Derived, never asserted. `chain_truncated` means ancestors were dropped, so it has
            # to describe what this stage actually shed: a payload pushed over the cap by an
            # oversized `node_type` can reach a later stage without the chain losing anything.
            chain_truncated=chain_truncated or len(stage.facts.project_chain) < len(full_facts.project_chain),
        )

    return None


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

        Every resolver degrades to a missing key rather than a failure: the caller is about to
        spend money, and a partial attribution beats none. The one failure is a payload that
        will not fit in a header even after being reduced.
        """
        facts = _AttributionFacts(
            project_chain=self._resolve_project_chain(),
            workflow_name=self._resolve_workflow_name(),
            node_type=_transmissible_or_none(request.node_type),
            engine_id=_transmissible_or_none(self._resolve_engine_id()),
            orchestrator_engine_id=_transmissible_or_none(self._resolve_orchestrator_engine_id()),
            session_id=_transmissible_or_none(self._resolve_session_id()),
        )

        encoding = _encode_attribution_header(facts)
        if encoding is None:
            return GetAttributionContextResultFailure(
                result_details=(
                    "Attempted to describe an outbound request for budget attribution. Failed because "
                    "the attribution payload could not be encoded into a header, even after being reduced "
                    "to its smallest form. The request can proceed, but this spend will not be attributed."
                )
            )

        # From `encoding.facts`, never `facts`: the reduction step can drop values, and these
        # fields must describe exactly what the header encodes.
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
        """Resolve the current project's ancestry as names, leaf-first, or [] when unavailable.

        `tags.project` is the only dimension the Cloud matches budgets against, so whatever
        goes here has to be something a budget admin can author into a rule. The project name
        is that: required by the schema, so always present, and the same string on every
        machine that opens the project.

        Two costs come with it, both accepted deliberately. A user-authored string rides in a
        header that SSL-inspecting egress proxies log, so a project name discloses whatever the
        user put in it -- a client name, typically. And a rename silently re-points that
        project's spend, because the Cloud matches on the string alone and has no way to know
        the two names are the same project.

        A nameless entry costs the whole chain, wherever it sits. `get_project_chain` maps any
        falsy name to None, so an entry is nameless either because its template did not load or
        because it loaded with an empty `name` -- nothing in the schema forbids the empty
        string. The two are indistinguishable here and need not be distinguished: both leave a
        link no budget rule can name.

        The synthetic `<system-defaults>` rest state is skipped by *id*, before its name is
        read. It does have a template -- the shipped default one -- so it does have a name,
        and that name is "Default Project", which is exactly the kind of generic string that
        would collide across every user in an org. It is also not a project the Cloud models
        at all: it is the root sentinel, never a real ancestor, so skipping it leaves a chain
        the Cloud can still match rather than a truncated one.

        A name the Cloud cannot store costs the *whole* chain, not just its own entry. Budget
        paths are root-anchored, so a chain missing any link already matches nothing -- and
        dropping only the offending entry is worse than sending none, because it promotes that
        entry's parent to leaf and bills a real ancestor project for spend it never incurred.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return []

        project_names: list[str] = []
        for entry in chain:
            if entry.id == SYSTEM_DEFAULTS_KEY:
                continue
            if entry.name is None:
                logger.warning(
                    "Dropping project attribution for this call: a project in the chain has no "
                    "usable name, so the chain cannot be described. Give every project a name to "
                    "attribute its spend."
                )
                return []
            project_names.append(entry.name)

        # A real project *named* `<system-defaults>`, separate from the sentinel id skipped
        # above. Nothing stops a user typing the reserved string into `name:`. The Cloud
        # reserves it and discards a client copy, which would drop that entry and bill its
        # parent for spend it never incurred, so the chain goes nowhere instead.
        if SYSTEM_DEFAULTS_KEY in project_names:
            logger.warning(
                "Dropping project attribution for this call: a project is named '%s', which Griptape "
                "Cloud reserves for its own use. Rename the project to attribute its spend.",
                SYSTEM_DEFAULTS_KEY,
            )
            return []

        untransmissible_count = sum(1 for name in project_names if not _is_transmissible(name))
        if untransmissible_count > 0:
            logger.warning(
                "Dropping project attribution for this call: %d project name(s) in the chain cannot be "
                "transmitted intact. This spend will land in the default budget.",
                untransmissible_count,
            )
            return []
        return project_names

    def _resolve_workflow_name(self) -> str | None:
        """Resolve the current workflow's registry key, or None when it cannot be determined.

        An unsaved workflow is registered under an `unsaved:<uuid4>` key that is fresh every
        session, so the sentinel goes out instead: one low-cardinality bucket for scratch spend,
        still distinguishable from an absent key. A saved key is workspace-path-derived, so it
        carries the same transmissibility risk as a project name and gets the same check.
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
        return _transmissible_or_none(registry_key)

    def _resolve_engine_id(self) -> str | None:
        """Resolve this engine's identifier, or None when it cannot be determined."""
        try:
            return self.engine.engine_identity_manager.engine_id
        except Exception:
            logger.warning("Could not resolve the engine id for budget attribution.", exc_info=True)
            return None

    def _resolve_orchestrator_engine_id(self) -> str | None:
        """Resolve the parent engine's id, set at spawn on workers and unset on the orchestrator.

        No guard: reading an environment variable cannot raise. In practice the key is always
        absent, since worker requests are forwarded to the orchestrator, which has no parent.
        """
        return os.getenv("GTN_ORCHESTRATOR_ENGINE_ID")

    def _resolve_session_id(self) -> str | None:
        """Resolve the active session id, or None when it cannot be determined."""
        try:
            return self.engine.session_manager.active_session_id
        except Exception:
            logger.warning("Could not resolve the session id for budget attribution.", exc_info=True)
            return None
