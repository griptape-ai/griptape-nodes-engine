"""The node bookkeeping of one open workflow."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowNodes:
    """Which flow each node of one open workflow belongs to, and which of its nodes run on a worker."""

    name_to_parent_flow_name: dict[str, str] = field(default_factory=dict)
    # node_name -> (target_request_id, worker_engine_id, worker_request_topic) for the
    # ExecuteNodeRequests currently routed to a worker. Used to send a CancelExecuteNodeRequest
    # to the right worker.
    orch_worker_requests: dict[str, tuple[str, str, str]] = field(default_factory=dict)
