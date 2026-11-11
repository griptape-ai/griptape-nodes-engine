- `RunWorkflowFromScratchRequest` and `RunWorkflowFromRegistryRequest` now ignore the envelope's
  `open_workflow_id`, so they no longer fail when it names a closed workflow.
  [#6198](https://github.com/griptape-ai/griptape-nodes-engine/issues/6198)
