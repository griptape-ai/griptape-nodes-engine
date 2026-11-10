- An MCP session without an `X-Griptape-Workflow-Id` header works on the workflow its latest call
  reported in `open_workflow`. If another client closes it, the session's calls fail with
  `WorkflowNotOpenResultFailure` until it opens another, instead of moving to another open workflow.
  [#6234](https://github.com/griptape-ai/griptape-nodes-engine/issues/6234)
