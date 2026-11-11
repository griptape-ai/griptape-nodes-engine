- `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` now fail for a saved workflow that
  isn't open, instead of opening it without its nodes, where the next save would erase the file.
  Open it with `OpenWorkflowRequest`.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
