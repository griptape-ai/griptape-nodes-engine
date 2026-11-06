- `RunWorkflowFromScratchRequest`, and `RunWorkflowWithCurrentStateRequest` when it opens the file
  as a new workflow, now close that workflow when the file fails to load, as
  `RunWorkflowFromRegistryRequest` does, instead of leaving a half-built workflow open.
