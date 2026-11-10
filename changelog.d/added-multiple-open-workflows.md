- The engine keeps several workflows open at once, one per editor tab. Requests that open a
  workflow, `SetWorkflowContextRequest` and `RunWorkflowFromScratchRequest` included, open it
  beside the others instead of failing or closing the open one. See
  [Transports](docs/guides/transports.md#open-workflows).
  [#6231](https://github.com/griptape-ai/griptape-nodes-engine/issues/6231)
