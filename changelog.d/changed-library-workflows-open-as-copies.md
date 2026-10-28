- **Breaking:** `SetWorkflowContextRequest`, `EnsureWorkflowAndFlowRequest`, and
  `ContextManager.push_workflow()` no longer open a workflow a library provides. Use
  `RunWorkflowFromRegistryRequest`, which opens an unsaved copy.
