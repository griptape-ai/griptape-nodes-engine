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


@dataclass
@PayloadRegistry.register
class GetAttributionContextRequest(RequestPayload):
    """Describe the current project so an outbound call can be attributed to it.

    The result is descriptive rather than secret: it carries project ids and no credential.

    It does carry user-authored strings. A project id has no enforced shape -- it is usually a
    slug of the project name, the user may replace it with any string, and a project created
    before ids existed carries the canonical path to its file. All of it is visible to an
    SSL-inspecting egress proxy.

    Attribution is best-effort. A project chain the engine cannot resolve is omitted rather
    than guessed at or raised, because the caller is about to spend money and an unattributed
    call beats a blocked one.

    Use when: A node or driver is about to make a credit-consuming call and wants the spend
    attributed to the project the user is working in.

    Results: GetAttributionContextResultSuccess (a header value is available) |
        GetAttributionContextResultFailure (none could be produced; the call should still
        proceed, unattributed)
    """

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

    `header_value` is `base64url(utf-8 JSON)` with padding kept. The decoded payload is
    `{"v": 1}` when no project is open, and otherwise `{"v": 1, "tags": {"project": [...]}}`.
    `project` is the only key the engine sends, because it is the only one the Cloud matches
    budgets against, and `tags` is the only namespace the Cloud parser reads.

    A missing `tags` is a real signal rather than an absence: it says no project is open on a
    client that speaks attribution, which the Cloud distinguishes from a client that sends no
    attribution header at all. `<system-defaults>` never travels -- the Cloud reserves that
    string for its own default and rejects a client copy, so the rest state simply omits it.

    `project_chain` is populated from the same pass that built `header_value`, so a consumer
    reading it and the Cloud reading the header cannot disagree.

    Args:
        header_value: The encoded header value to send
        header_name: The header to send it under. Shipped on the result so a rename never has
            to touch a vendored client copy.
        schema_version: The payload schema version encoded in `header_value`
        project_chain: The project ids the call is attributed to, ordered leaf-first, each one
            exactly as the project stores it -- unstripped, uncut, and never repaired. Depth
            and length are the Cloud's to judge, and it reports what it had to repair. Empty
            when no project is open or the chain could not be read.
    """

    header_value: str
    header_name: str = ATTRIBUTION_HEADER_NAME
    schema_version: int = ATTRIBUTION_SCHEMA_VERSION
    project_chain: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """No attribution header value could be produced.

    The only cause is a project id the wire cannot carry: an id derived from a filesystem path
    whose bytes are not valid UTF-8 arrives carrying lone surrogates, which cannot be encoded
    at all. There is no honest partial form to send instead, so the whole header is given up.
    The caller should proceed and send no attribution header -- the spend lands in the default
    budget.
    """
