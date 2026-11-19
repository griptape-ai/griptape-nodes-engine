- **Breaking:** `FlowManager` methods that read the current workflow, such as
  `check_for_existing_running_flow`, and the `global_*` properties are removed. Use the matching
  `_in(workflow)` method. `WorkflowManager.run_workflow` now requires a `workflow` argument.
