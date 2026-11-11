- Closing an unsaved workflow now removes it from `ListAllWorkflowsRequest`, and its `workflow_id`
  fails with `WorkflowNotFoundResultFailure` instead of opening an empty canvas.
  [#6232](https://github.com/griptape-ai/griptape-nodes-engine/issues/6232)
