- The `WorkflowRegistry` classmethods `generate_new_workflow`, `ensure_unsaved`,
  `delete_workflow_by_name`, `clear_user_workflows`, and `rekey_workflow` are deprecated and log a
  warning when called. They will be removed in 0.110.0. Use `RegisterWorkflowRequest`,
  `SetWorkflowContextRequest`, `DeleteWorkflowRequest`, `RefreshWorkflowRegistryRequest`, and
  `MoveWorkflowRequest` or `RenameWorkflowRequest` instead. Until then, three behave differently:
  `generate_new_workflow` with a `file_path` names the workflow after its file, not `registry_key`;
  `ensure_unsaved` needs a key of `unsaved:` followed by a workflow id; and `rekey_workflow` needs
  the file to be at its new path already.
