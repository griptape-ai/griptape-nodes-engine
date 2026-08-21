"""Execution lease protocol: the wire contract between an engine and its admission authority.

On a shared GPU machine, several engines (one per artist) coordinate through an
admission authority -- the Load Balancer -- that decides when each engine's
execution may start. The engine acquires an **execution lease** before starting
real work (a workflow run, or a single-node run including its upstream dirty
nodes), holds it for the whole run, and releases it when the run ends. The
grant is time-bound: it is renewed while in use and reclaimed if its engine
crashes or goes silent.

These five events are the **entire public contract** between the two sides.
The reference Load Balancer lives in its own repository and depends only on
this module through the published engine package; a studio replacing it with
its own admission policy (priority tiers, render-farm integration) implements
the other side of exactly these events. Everything else -- queue order,
fairness, capacity reasoning -- is an implementation detail of whichever
balancer is running, deliberately NOT part of the contract.

Versioning follows the workflow-schema house style: ``schema_version`` is a
semver string stamped by the sender, with the latest version declared beside
the events (mirroring ``WorkflowMetadata.LATEST_SCHEMA_VERSION``). While the
protocol is 0.x, a **minor** version mismatch is breaking (caret semantics):
the balancer rejects the acquire with a message naming both versions. Adding
optional fields is compatible and needs no bump; events cross the wire without
``forbid_extra_keys``, so older peers ignore fields they do not know.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class AcquireExecutionLeaseRequest(RequestPayload):
    """Sent by an engine to the admission authority before starting an execution.

    The response deliberately arrives **only when the lease is granted** -- an
    engine waiting its turn simply has an unresolved request. There is no
    wall-clock timeout on the wait: queue waits behind long AI workloads are
    legitimately unbounded, and balancer liveness is enforced by the transport
    link, not per-request ceilings. A waiting engine abandons its place with
    CancelExecutionLeaseRequest.

    The balancer must reject an acquire from an engine that already holds or is
    already waiting on a lease: engines dispatch inbound requests as
    independent tasks, so two near-simultaneous run requests can both pass the
    engine's own single-run guard and both attempt to acquire.

    Args:
        engine_id: The engine requesting admission.
        session_id: The session the execution belongs to, or None if the
            engine has no active session yet.
        scope: Short description of what the lease covers, for status display
            and balancer logs -- e.g. "workflow" or "single_node". Free-form;
            carries no admission semantics in this protocol version.
        schema_version: Lease-protocol version the sender was built against.
            Defaults to this package's latest.
        requirements: Optional resource requirements for the execution, in
            ResourceManager's requirements vocabulary (capability key to
            required value/comparator). Advisory: declared needs may be
            absent, stale, or wrong, so a capacity-aware balancer admits on
            observed capacity and treats these as hints.
        machine_id: The machine this engine runs on, or None when unknown.
            Constant in a single-machine deployment; carried from day one so a
            multi-machine balancer can group leases per machine without a
            protocol change (an engine cannot migrate, so its machine is fixed
            at spawn).
    """

    LATEST_SCHEMA_VERSION: ClassVar[str] = "0.1.0"

    engine_id: str
    session_id: str | None = None
    scope: str = "workflow"
    schema_version: str = LATEST_SCHEMA_VERSION
    requirements: dict[str, Any] | None = None
    machine_id: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class AcquireExecutionLeaseResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The lease is granted; the engine may start executing.

    Args:
        lease_id: Handle for the granted lease. All subsequent renew, release,
            and cancel calls name it.
    """

    lease_id: str


@dataclass
@PayloadRegistry.register
class AcquireExecutionLeaseResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The lease was refused (not queued).

    Distinct from waiting: refusal means the balancer will never grant this
    request -- the engine already holds or is waiting on a lease, the
    schema_version is incompatible, or the deployment is not entitled to
    managed execution. result_details carries the reason.
    """


@dataclass
@PayloadRegistry.register
class ReleaseExecutionLeaseRequest(RequestPayload):
    """Sent by an engine to return its lease after an execution ends.

    Sent by the engine-side release watchdog -- never tied to request-handler
    control flow -- after execution-scoped memory has been torn down, so the
    next admitted engine starts against a reclaimed machine. Release after the
    balancer already reclaimed the lease (crash eviction won a race) fails
    softly; the engine treats that failure as already-released.

    Args:
        lease_id: The lease being returned.
    """

    lease_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class ReleaseExecutionLeaseResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The lease was returned; the balancer may admit the next execution."""


@dataclass
@PayloadRegistry.register
class ReleaseExecutionLeaseResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The lease could not be released -- unknown or already reclaimed."""


@dataclass
@PayloadRegistry.register
class RenewExecutionLeaseRequest(RequestPayload):
    """Sent periodically by the lease holder to keep its lease alive.

    The backstop for a hung-but-alive engine: process-exit and transport-drop
    detection catch dead engines, renewal catches one that is running but
    wedged. Lease TTLs must be generous -- diffusion runs are long -- so a
    missed renewal is a strong signal, not a latency artifact.

    Args:
        lease_id: The lease to renew.
    """

    lease_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class RenewExecutionLeaseResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The lease TTL was extended."""


@dataclass
@PayloadRegistry.register
class RenewExecutionLeaseResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The lease could not be renewed -- unknown, expired, or reclaimed.

    The holder must treat this as lease loss: stop claiming admission,
    release/tear down through the normal watchdog path, and re-acquire before
    any further execution.
    """


@dataclass
@PayloadRegistry.register
class CancelExecutionLeaseRequest(RequestPayload, SkipTheLineMixin):
    """Abandon a lease: a waiting acquire gives up its place in line.

    Uses SkipTheLineMixin so the cancel is processed immediately wherever it
    lands -- it must never queue behind the very acquire it is cancelling on a
    receiver that processes events in order.

    Cancelling a lease that is already *held* is equivalent to releasing it;
    balancers should honor either verb so a cancel racing a grant cannot strand
    the lease.

    Args:
        lease_id: The lease (granted or still waiting) to abandon.
    """

    lease_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class CancelExecutionLeaseResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The waiting acquire was abandoned (or the held lease released)."""


@dataclass
@PayloadRegistry.register
class CancelExecutionLeaseResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The lease could not be cancelled -- unknown to the balancer."""


@dataclass
class ExecutionAdmissionStatusEntry:
    """One engine's standing in the admission state, for status display.

    Args:
        engine_id: The engine this entry describes.
        position: 1-based place in line under an ordered (FIFO) policy, or
            None when the running policy defines no stable order -- consumers
            must render position-less entries gracefully.
        scope: The scope string from the entry's acquire request.
    """

    engine_id: str
    position: int | None = None
    scope: str = "workflow"


@dataclass
@PayloadRegistry.register
class ExecutionAdmissionStatusEvent(AppPayload):
    """Broadcast by the balancer on every admission-state change.

    Sent to each connected engine over its balancer link; the engine relays it
    to its own clients on its session topic so artists see their place in line
    without holding a balancer connection. At-most-once, last-write-wins
    display data: a missed event is corrected by the next state change.

    Args:
        entries: Standing of every engine currently waiting for admission.
    """

    entries: list[ExecutionAdmissionStatusEntry] = field(default_factory=list)
