"""Events for budget attribution.

The engine is the only party that knows which project a credit-consuming call
belongs to: Griptape Cloud deliberately holds no project model and no hierarchy.
These events let a node about to spend money ask the engine for one ready-to-send
header value describing that attribution, and stop there. Nothing here makes a
network call, holds a credential, or enforces a budget.
"""

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

ATTRIBUTION_HEADER_NAME = "X-Griptape-Attribution"
ATTRIBUTION_SCHEMA_VERSION = 1

# Sent as the `workflow` value when the current workflow has never been saved. A real value,
# not an omission: it buckets scratch spend without leaking the per-session `unsaved:<uuid4>`
# registry key. Lives here, beside the header name, because it is part of the wire contract
# the vendored client helper and the Cloud both read.
UNSAVED_WORKFLOW_SENTINEL = "<unsaved>"


@dataclass
@PayloadRegistry.register
class GetAttributionContextRequest(RequestPayload):
    """Describe the current engine context so an outbound call can be attributed to a project.

    The result carries one encoded header value naming the project chain the caller
    belongs to, plus the same facts as structured fields. Everything in it is
    descriptive rather than secret: project ids, a workflow key, a node type, and
    engine/session guids -- identifiers the user already sees in the editor. It
    carries no credential and no user-authored label.

    Attribution is best-effort by design. Anything the engine cannot determine is
    omitted from the payload rather than guessed at or raised, because the caller is
    about to spend money and a missing dimension is better than a blocked call.

    Use when: A node or driver is about to make a credit-consuming call and wants
    the spend attributed to the project the user is working in.

    Args:
        node_type: The library-declared node type making the call (e.g.
            "GriptapeProxyImage"), when the caller knows it. The engine cannot read
            this reliably -- the caller is usually a driver inside process(), where
            the node is not on the context stack -- so the caller passes it.

    Results: GetAttributionContextResultSuccess (a header value is available) |
        GetAttributionContextResultFailure (no header value could be produced; the
        call should still proceed, unattributed)
    """

    node_type: str | None = None
    # Per-call and useful only to the direct caller. Without this the Success payload --
    # header_value included -- would be queued for broadcast on the WebSocket feed once
    # per metered call (engine.py handle_request).
    #
    # `field(default=False, kw_only=True)`, not a bare `= False`: `RequestPayload` is
    # `kw_only=True` but these subclasses are plain `@dataclass`, so a bare redeclaration
    # re-registers the field as positional and reorders __init__. Precedent:
    # `agent_events.py` CancelAgentRequest.
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """An attribution header value is available; attach it to the outbound request.

    `header_value` is `base64url(utf-8 JSON)` with padding kept. The decoded payload
    omits any key the engine could not determine, so an absent key always means
    "unknown" and never "none". Two values are real rather than omissions:
    `<system-defaults>` in the project chain means no project is open, and
    `<unsaved>` as the workflow means the current workflow has never been saved.

    The structured fields describe exactly what `header_value` encodes -- they are
    populated from the same encoding pass, so a consumer reading them and the Cloud
    reading the header cannot disagree.

    Args:
        header_value: The encoded header value to send
        header_name: The header to send it under. Shipped on the result so a rename
            never has to touch a vendored client copy.
        schema_version: The payload schema version encoded in `header_value`
        project_chain: The project ids the call is attributed to, ordered leaf-first.
            Ids only; project names are never included.
        workflow_name: The current workflow's registry key, or `<unsaved>`
        node_type: The node type the caller passed back, unchanged
        engine_id: The id of the engine that answered
        orchestrator_engine_id: The parent engine's id when this engine is a worker
        session_id: The active session id
        chain_truncated: Whether ancestors were dropped from `project_chain`
    """

    header_value: str
    header_name: str = ATTRIBUTION_HEADER_NAME
    schema_version: int = ATTRIBUTION_SCHEMA_VERSION
    project_chain: list[str] = field(default_factory=list)
    workflow_name: str | None = None
    node_type: str | None = None
    engine_id: str | None = None
    orchestrator_engine_id: str | None = None
    session_id: str | None = None
    chain_truncated: bool = False


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """No attribution header value could be produced.

    The only cause is a payload that stays over the header size limit even after
    being reduced to its smallest form. Every other degradation returns Success with
    a key omitted, because a partial attribution beats none.

    The caller should proceed with the outbound call and send no attribution header.
    The spend still happens; it lands in the default budget instead of a project's.
    """
