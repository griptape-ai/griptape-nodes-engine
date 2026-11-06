- `RunWorkflowFromScratchRequest` and `RunWorkflowFromRegistryRequest` ignore the envelope's
  `open_workflow_id`, like `SetWorkflowContextRequest`, so they no longer fail because the workflow
  the client named has closed.
  [#6198](https://github.com/griptape-ai/griptape-nodes-engine/issues/6198)
