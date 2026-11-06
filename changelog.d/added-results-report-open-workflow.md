- `SetWorkflowContextSuccess`, `SaveWorkflowResultSuccess`, `RunWorkflowFromRegistryResultSuccess`
  and `RunWorkflowFromScratchResultSuccess` now carry `open_workflow`, the workflow open after the
  request, in the form `EngineHeartbeatResultSuccess.open_workflows` uses.
  `RunWorkflowWithCurrentStateResultSuccess` carries it when the request opened the file as a new
  workflow, and `EnsureWorkflowAndFlowResultSuccess` when it opened one. After a save, it tells whether the open workflow moved onto the copy, which a save of a
  Griptape-provided workflow does.
  [#6199](https://github.com/griptape-ai/griptape-nodes-engine/issues/6199)
