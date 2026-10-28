- A `ForEach Group`, `For Loop Group`, or `Retry Group` no longer fails to start with "Failed while
  restoring the saved value of '<node>.<parameter>'" when a body node's connected input holds a value
  that its own range no longer allows, such as a crop coordinate left over from a wider image. The
  loop body copy now leaves out values that its connections supply, in both execution modes. As a
  result, a body node's `validate_before_workflow_run` sees a connected input's default rather than
  the value shown on the canvas, the same as a node at the top level of the workflow.
  [#5889](https://github.com/griptape-ai/griptape-nodes-engine/issues/5889)
