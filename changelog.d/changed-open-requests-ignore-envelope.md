- `RunWorkflowFromScratchRequest` and `RunWorkflowFromRegistryRequest` now ignore the envelope's
  `open_workflow_id`, like `SetWorkflowContextRequest`, so they no longer fail when the workflow it
  names has closed.
  [#6198](https://github.com/griptape-ai/griptape-nodes-engine/issues/6198)
