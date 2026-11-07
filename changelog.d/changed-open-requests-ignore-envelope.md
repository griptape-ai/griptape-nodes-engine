- `RunWorkflowFromScratchRequest` and `RunWorkflowFromRegistryRequest` now ignore the envelope's
  `open_workflow_id`, as `SetWorkflowContextRequest` does, so an open no longer fails because the
  workflow the client named has closed. A clean slate closes the open workflow, whichever workflow
  the envelope named.
  [#6198](https://github.com/griptape-ai/griptape-nodes-engine/issues/6198)
