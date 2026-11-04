- **Breaking:** `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` now open a new
  unsaved workflow only from a `workflow_name` of `unsaved:` followed by 32 lowercase hex digits,
  the form the engine mints. Any other `unsaved:` name fails. Leave `workflow_name` out to have the
  engine pick one, and read it from the result.
