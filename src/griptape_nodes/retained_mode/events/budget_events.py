"""Events for budget attribution.

Griptape Cloud holds no project model, so the engine is the only party that can say
which project a credit-consuming call belongs to. These events hand a caller one
ready-to-send header value describing that. Nothing here makes a network call, holds
a credential, or enforces a budget.
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

# Sent as the `workflow` value when the workflow has never been saved. A real value, not an
# omission: it buckets scratch spend without leaking the per-session `unsaved:<uuid4>` key.
UNSAVED_WORKFLOW_SENTINEL = "<unsaved>"


@dataclass
@PayloadRegistry.register
class GetAttributionContextRequest(RequestPayload):
    """Describe the current engine context so an outbound call can be attributed to a project.

    Everything in the result is descriptive rather than secret: project names, a workflow key,
    a node type, and engine/session guids. It carries no credential.

    It does carry user-authored strings. `tags.project` holds project names, and the workflow
    key is a workspace-relative path. Both are visible to an SSL-inspecting egress proxy.

    Attribution is best-effort. Anything the engine cannot determine is omitted rather than
    guessed at or raised, because the caller is about to spend money and a missing dimension
    beats a blocked call.

    Use when: A node or driver is about to make a credit-consuming call and wants the spend
    attributed to the project the user is working in.

    Args:
        node_type: The library-declared node type making the call (e.g. "GriptapeProxyImage"),
            when the caller knows it. The engine cannot read this reliably -- the caller is
            usually a driver where the node is not on the context stack -- so the caller passes it.

    Results: GetAttributionContextResultSuccess (a header value is available) |
        GetAttributionContextResultFailure (none could be produced; the call should still
        proceed, unattributed)
    """

    node_type: str | None = None
    # Per-call and useful only to the direct caller; without this the Success payload would be
    # broadcast on the WebSocket feed once per metered call.
    #
    # `field(default=False, kw_only=True)` rather than a bare `= False`: `RequestPayload` is
    # `kw_only=True` but this subclass is a plain `@dataclass`, so a bare redeclaration would
    # re-register the field as positional and reorder __init__.
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """An attribution header value is available; attach it to the outbound request.

    `header_value` is `base64url(utf-8 JSON)` with padding kept. Every dimension sits under a
    single `tags` object -- the Cloud parser reads no other namespace. The decoded payload omits
    any key the engine could not determine, so an absent key means "unknown" and never "none".
    `<unsaved>` as the workflow is a real value rather than an omission: it means the workflow has
    never been saved. `<system-defaults>` never travels -- the Cloud reserves that string for its
    own default and rejects a client copy, so an unprojected call simply omits `project`.

    The structured fields are populated from the same encoding pass as `header_value`, so a
    consumer reading them and the Cloud reading the header cannot disagree.

    Args:
        header_value: The encoded header value to send
        header_name: The header to send it under. Shipped on the result so a rename never has
            to touch a vendored client copy.
        schema_version: The payload schema version encoded in `header_value`
        project_chain: The project names the call is attributed to, ordered leaf-first. Never
            partial and never shortened -- budget paths are root-anchored, so a chain missing a
            link would present a nested project as a root, and a name cut here would arrive
            looking intact. Depth and length are the Cloud's to judge, and it reports what it
            had to repair. Empty whenever the full ancestry cannot be described or sent.
        workflow_name: The current workflow's registry key, or `<unsaved>`
        node_type: The node type the caller passed back, unchanged
        engine_id: The id of the engine that answered
        orchestrator_engine_id: Reserved; not emitted in practice. Worker requests are
            forwarded to the orchestrator, which has no parent, so this is always absent
            today -- do not build a consumer-side join on it
        session_id: The active session id
        chain_truncated: Whether a chain the engine had resolved was given up to fit the
            header. Only ever set with an empty `project_chain`, because the chain is shed
            whole or not at all -- there is no partial form. Empty and unflagged means either
            no project is open or the chain could not be described; both send no `project`
            key, and neither is a size problem.
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

    In practice the only cause is a header still over the size the ingress will carry even with
    the project chain given up; every other degradation returns Success with a key omitted. An
    unencodable payload lands here too, but every field that could carry one is filtered first,
    so that path is a backstop. The caller should proceed and send no attribution header -- the
    spend lands in the default budget.
    """
