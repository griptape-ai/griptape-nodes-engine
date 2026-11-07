- The "Multiple open workflows" engine beta feature (`multiple_open_workflows`) lets the engine keep
  several workflows open at once. With it on, `OpenWorkflowRequest`, `SetWorkflowContextRequest`,
  `RunWorkflowFromRegistryRequest` and `RunWorkflowFromScratchRequest` open a workflow beside the
  others instead of failing or closing the open one. See
  [Transports](docs/guides/transports.md#open-workflows).
  [#6231](https://github.com/griptape-ai/griptape-nodes-engine/issues/6231)
