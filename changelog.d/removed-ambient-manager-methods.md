- **Breaking:** Manager methods that acted on whichever workflow was open are removed. Call the `_in`
  variant with the node's workflow, for example `FlowManager.get_parent_flow_in(self.workflow, name)`.
  `cancel_flow_run` becomes `cancel_all_runs_in(workflow)`, and the `ContextManager` path getters
  become `absolute_path` and `working_directory` on `get_current_workflow()`.
  `FlowManager.cancel_run` now requires `flows`; use `cancel_run_in(workflow, run_id)`.
  Removed: `ObjectManager.add_object_by_name`, `del_obj_by_name`; `FlowManager.get_parent_flow`,
  `reparent_flow`, `flow_state`, `unresolve_all_nodes_in_flow`, `clear_execution_queue`,
  `cancel_flow_run`, `has_connection`, `get_connections_on_node`, `get_connected_output_parameters`,
  `get_connected_input_parameters`, `get_connected_input_from_node`,
  `get_connected_output_from_node`, `get_start_node_queue`, `get_start_node_from_node`,
  `get_prev_node`; `ContextManager.get_current_workflow_file_path`,
  `get_current_workflow_working_directory`.
