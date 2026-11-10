- `SetWorkflowContextSuccess`, `SaveWorkflowResultSuccess`, `RunWorkflowFromRegistryResultSuccess`
  and `RunWorkflowFromScratchResultSuccess` now carry `open_workflow`, the workflow open after the
  request, as `ListOpenWorkflowsRequest` lists it.
  `RunWorkflowWithCurrentStateResultSuccess` and `EnsureWorkflowAndFlowResultSuccess` carry it
  too, whether they used the open workflow the envelope named or opened a new one. After a save, it
  tells whether the open workflow moved onto the copy, which a save of a Griptape-provided workflow
  does.
  [#6199](https://github.com/griptape-ai/griptape-nodes-engine/issues/6199)
