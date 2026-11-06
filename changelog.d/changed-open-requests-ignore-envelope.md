- `RunWorkflowFromScratchRequest` and `RunWorkflowFromRegistryRequest` now ignore the envelope's
  `open_workflow_id`, as `SetWorkflowContextRequest` does, so an open no longer fails because the
  workflow the client named has closed. They close the workflow named in the new
  `replaces_workflow_id` field first, or the open workflow when that is left out.
  [#6198](https://github.com/griptape-ai/griptape-nodes-engine/issues/6198)
