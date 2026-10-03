"""Events for budget attribution and direct-to-provider spend.

The engine is the only party that knows which project a credit-consuming call belongs to.
`GetAttributionContextRequest` hands a caller one ready-to-send header value for calls that go
through Griptape Cloud. A node calling a provider directly (BYOK) bypasses Cloud, so it asks
first with `BudgetAccessRequest` and reports what it spent with `ReportUsageRequest`.
"""

from dataclasses import dataclass, field
from typing import Any

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

    The result carries project ids and no credential. Ids are user-influenced -- usually a slug
    of the project name, replaceable with any string, and a filesystem path on a project created
    before ids existed -- and all of it is visible to an SSL-inspecting egress proxy.

    Best-effort: nothing here raises. Not knowing which project to bill is not a reason to refuse
    work -- the spend is legitimate, it just lands unattributed. A chain that cannot be read
    yields a Failure rather than an empty chain, which would assert that no project is open.

    Use when: A node or driver is about to make a credit-consuming call and wants the spend
    attributed to the project the user is working in.

    Results: GetAttributionContextResultSuccess (a header value is available) |
        GetAttributionContextResultFailure (none could be produced; the call should still
        proceed, unattributed)
    """

    # Per-call and useful only to the direct caller; otherwise the Success payload broadcasts on
    # the WebSocket feed once per metered call.
    #
    # `field(default=False, kw_only=True)` rather than a bare `= False`: `RequestPayload` is
    # `kw_only=True` but this subclass is a plain `@dataclass`, so a bare redeclaration would
    # re-register the field as positional and reorder __init__.
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """An attribution header value is available; attach it to the outbound request.

    `header_value` is `base64url(utf-8 JSON)` with padding kept, decoding to `{"v": 1}` when no
    project is open and `{"v": 1, "tags": {"project": [...]}}` otherwise. The bare envelope is
    sent for forward-compatibility rather than as a signal. `<system-defaults>` never travels.

    Args:
        header_value: The encoded header value to send
        header_name: The header to send it under. On the result so a rename never has to touch a
            vendored client copy.
        schema_version: The payload schema version encoded in `header_value`
        project_chain: The project ids the call is attributed to, leaf-first, each exactly as
            stored -- unstripped, uncut, never repaired. Populated from the same pass that built
            `header_value`, so the two cannot disagree. Empty only when no project is open; a
            chain that could not be read yields a Failure instead.
    """

    header_value: str
    header_name: str = ATTRIBUTION_HEADER_NAME
    schema_version: int = ATTRIBUTION_SCHEMA_VERSION
    project_chain: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class GetAttributionContextResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """No attribution header value could be produced; send no header and make the call anyway.

    Two causes, and both mean the engine cannot describe the spend truthfully. The project chain
    could not be read, so whether a project is open is unknown. Or a project id derived from a
    filesystem path whose bytes are not valid UTF-8 holds lone surrogates that cannot be encoded.

    Sending nothing rather than `{"v": 1}` keeps the engine from asserting a fact it does not
    have. The loss is visible only in the engine's log.
    """


@dataclass
@PayloadRegistry.register
class BudgetAccessRequest(RequestPayload):
    """Ask Griptape Cloud whether a direct provider call fits the budgets it is attributed to.

    Fails open: if Cloud cannot be reached or gives no usable answer, the call is cleared with
    `checked=False`. Only an explicit deny from Cloud fails. A network blip must not halt work
    the org may not even be billed for, and an offline bypass of self-declared spend cannot be
    enforced anyway.

    Use when: A node is about to call a model provider directly, not through the Griptape proxy.

    Args:
        model_id: Stable catalog key of the model being invoked (e.g., "gtc_claude_opus_4_7")
        estimated_cost_micro_usd: Expected cost of the call. When omitted, the call is refused
            only by budgets with no headroom left.
        node_type: Library class of the calling node, for attribution
        node_id: Opaque id of the calling node, for attribution. Never the node's label.

    Results: BudgetAccessResultSuccess (cleared to proceed) |
        BudgetAccessResultFailure (a budget refused the call; the node should not make it)
    """

    model_id: str
    estimated_cost_micro_usd: int | None = None
    node_type: str | None = None
    node_id: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class BudgetAccessResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The call is cleared; make it, then report it with `ReportUsageRequest`.

    Args:
        correlation_id: Pass this on `ReportUsageRequest` so Cloud can pair the check with the report
        checked: False when Cloud could not be asked and the call was cleared without a check
        effective_remaining_credits: The tightest headroom across every matching budget, when checked
    """

    correlation_id: str
    checked: bool
    effective_remaining_credits: int | None = None


@dataclass
@PayloadRegistry.register
class BudgetAccessResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """A budget refused the call. The node should not make it.

    `result_details` is the halt message naming every budget that refused, and `exception` is a
    `BudgetExceededError`; raise it from the node so the run stops with that message.

    Args:
        blocked_by: Cloud's entry for every budget that refused, as sent
        correlation_id: The id the check was made under
    """

    blocked_by: list[dict[str, Any]]
    correlation_id: str


@dataclass
@PayloadRegistry.register
class ReportUsageRequest(RequestPayload):
    """Report what a direct provider call cost, so it counts against budgets and shows in usage.

    Best-effort: the report is sent in the background, retried briefly, and dropped with a log
    line if Cloud cannot take it. A node must never fail over its usage report.

    Use when: A node has just finished a direct provider call (not through the Griptape proxy).

    Args:
        declared_cost_micro_usd: What the call cost, in millionths of a US dollar
        provider: Provider name, e.g. "anthropic"
        model: Model name or catalog key
        activity_type: Kind of call, e.g. "chat_completion"
        node_type: Library class of the calling node, for attribution
        node_id: Opaque id of the calling node, for attribution. Never the node's label.
        correlation_id: From the `BudgetAccessResultSuccess` of the check before this call, if any

    Results: ReportUsageResultSuccess (queued) | ReportUsageResultFailure (not sent)
    """

    declared_cost_micro_usd: int
    provider: str | None = None
    model: str | None = None
    activity_type: str | None = None
    node_type: str | None = None
    node_id: str | None = None
    correlation_id: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class ReportUsageResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The report is queued. Delivery is not confirmed.

    Args:
        idempotency_key: The key the report is sent under, the same on every retry
    """

    idempotency_key: str


@dataclass
@PayloadRegistry.register
class ReportUsageResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The report was not sent. Never fail a node over this."""
