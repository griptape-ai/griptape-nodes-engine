- `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` no longer open a saved workflow
  without its nodes, which made the next save erase the file.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
