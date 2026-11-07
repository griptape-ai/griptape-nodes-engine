- Closing an unsaved workflow now removes it from `ListAllWorkflowsRequest`. Its nodes are gone once
  it closes, so its `workflow_id` now fails with `WorkflowNotFoundResultFailure` instead of opening
  an empty canvas.
  [#6232](https://github.com/griptape-ai/griptape-nodes-engine/issues/6232)
