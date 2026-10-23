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

    def parent_flow_name(self, node_name: str) -> str:
        """The name of the flow the node belongs to.

        Raises:
            KeyError: No node of that name is in this workflow.
        """
        if node_name not in self.name_to_parent_flow_name:
            msg = f"Node '{node_name}' could not be found."
            raise KeyError(msg)
        return self.name_to_parent_flow_name[node_name]

    def rename_node(self, old_name: str, new_name: str) -> None:
        """Record that the node `old_name` is now called `new_name`, in the same flow."""
        self.name_to_parent_flow_name[new_name] = self.name_to_parent_flow_name.pop(old_name)

    def rename_flow(self, old_name: str, new_name: str) -> None:
        """Point every node that belonged to the flow `old_name` at `new_name`."""
        for node_name, parent_flow_name in self.name_to_parent_flow_name.items():
            if parent_flow_name == old_name:
                self.name_to_parent_flow_name[node_name] = new_name
