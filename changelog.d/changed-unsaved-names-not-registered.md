- `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` fail for an `unsaved:` name the
  engine doesn't hold instead of creating a workflow for it. Reopen an unsaved workflow with
  `OpenWorkflowRequest` and its `workflow_id`.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
