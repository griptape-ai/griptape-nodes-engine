- `RunWorkflowFromScratchRequest` and `RunWorkflowWithCurrentStateRequest` now close the workflow
  they opened when its file fails to load, instead of leaving it half built.
  [#6260](https://github.com/griptape-ai/griptape-nodes-engine/issues/6260)
