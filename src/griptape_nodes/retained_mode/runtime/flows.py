"""The flow hierarchy, connections, and run state of one open workflow."""

from __future__ import annotations

from queue import Queue
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.machines.dag_builder import DagBuilder
from griptape_nodes.retained_mode.events.base_events import build_execution_event
from griptape_nodes.retained_mode.managers.run_queue import RunQueue

if TYPE_CHECKING:
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.machines.control_flow import ControlFlowMachine
    from griptape_nodes.retained_mode.events.base_events import ExecutionGriptapeNodeEvent, ExecutionPayload
    from griptape_nodes.retained_mode.managers.flow_manager import QueueItem
    from griptape_nodes.retained_mode.managers.run_queue import Run
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


class WorkflowFlows:
    """The flows of one open workflow, how they nest, how their nodes connect, and the run in progress."""

    def __init__(self, workflow: WorkflowRuntime | None = None) -> None:
        """Make empty run state for `workflow`, or for a line of runs that belongs to no workflow."""
        self._workflow = workflow
        self.name_to_parent_name: dict[str, str | None] = {}
        self.flow_to_referenced_workflow_name: dict[ControlFlow, str] = {}
        # Loop iteration flow name -> the flow holding the loop that runs it. Held in
        # memory only.
        self.iteration_flow_to_owner_flow_name: dict[str, str] = {}
        self.connections = Connections()

        self.global_flow_queue: Queue[QueueItem] = Queue()
        self.global_control_flow_machine: ControlFlowMachine | None = None
        self.global_single_node_resolution = False
        self.global_dag_builder = DagBuilder(self.connections)

        # The runs waiting for this workflow's machine and the one that has it. Read and written without a
        # lock, so only the engine loop may touch them. A run request sent synchronously from another
        # thread can reach them from that thread's loop, which is not supported yet (#6039).
        self.run_queue = RunQueue()
        self.run_in_progress: Run | None = None
        # True from when the run in progress starts until its request returns. Until then only the run's
        # driver may end it: its flow may not have started yet (still validating) or may have just been
        # cancelled, and the paths that end a paused run must not mistake either for the run being over.
        self.driving = False
        # True while cancel_all_runs_in awaits the run in progress, so a run submitted meanwhile waits to be turned away.
        self.cancelling_all = False

    def execution_event(self, payload: ExecutionPayload) -> ExecutionGriptapeNodeEvent:
        """An execution event about a run here, naming the workflow it belongs to, or none."""
        workflow_id = None
        if self._workflow is not None:
            workflow_id = self._workflow.workflow_id
        return build_execution_event(payload, workflow_id)
