- `SetWorkflowContextRequest` now opens a workflow in place of the open one instead of failing, the
  same way `RunWorkflowFromRegistryRequest` does. Name the workflow to close in
  `replaces_workflow_id`, or leave it out to close the open workflow. Send `create_flow=True` to
  also create the workflow's top-level flow. Setting the context to the workflow that is already
  open fails rather than emptying its canvas.
  [#6204](https://github.com/griptape-ai/griptape-nodes-engine/issues/6204)
