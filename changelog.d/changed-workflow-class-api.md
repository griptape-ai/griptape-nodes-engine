- **Breaking:** The `Workflow` class in `griptape_nodes.node_library.workflow_registry` changed for
  node libraries that use it. `Workflow.file_path` is read-only: move a workflow's file with
  `MoveWorkflowRequest`. The constructor now takes a `workflow_id`, and `Workflow.from_disk` is
  gone: register a workflow file with `RegisterWorkflowRequest`. The registry's
  `clear_all_workflows` is gone: rescan the workspace with `RefreshWorkflowRegistryRequest`.
