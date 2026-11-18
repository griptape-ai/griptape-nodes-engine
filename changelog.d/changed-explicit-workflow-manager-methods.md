- **Breaking:** `FlowManager` no longer has the methods that answered for the current workflow:
  `current_run`, `check_for_existing_running_flow`, `execution_is_advancing`,
  `is_execution_queue_empty`, `get_next_node_from_execution_queue`, `get_all_connected_nodes` and
  `is_node_connected`, nor the `global_*` execution state properties. Node libraries and apps that
  called them should use the matching `_in` method with the workflow, such as
  `check_for_existing_running_flow_in(workflow)`. `WorkflowManager.run_workflow` now requires a
  `workflow` argument: pass the workflow to build into, or `None` to open the file as a new one.
