- The `WorkflowRegistry` classmethods `generate_new_workflow`, `ensure_unsaved`,
  `delete_workflow_by_name`, `clear_user_workflows`, and `rekey_workflow` are deprecated and log a
  warning when called. They will be removed in 0.110.0. Use `RegisterWorkflowRequest`,
  `SetWorkflowContextRequest`, `DeleteWorkflowRequest`, `RefreshWorkflowRegistryRequest`, and
  `MoveWorkflowRequest` or `RenameWorkflowRequest` instead.
