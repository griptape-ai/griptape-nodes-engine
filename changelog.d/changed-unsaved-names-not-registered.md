- **Breaking:** `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` no longer create a
  workflow for an `unsaved:` name the engine doesn't hold. They fail as for any unknown name. To
  reopen an unsaved workflow, send `OpenWorkflowRequest` with its `workflow_id`, and treat
  `WorkflowNotFoundResultFailure` as the workflow being lost.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
