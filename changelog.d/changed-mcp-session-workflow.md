- An MCP session that sends no `X-Griptape-Workflow-Id` header now works on the workflow its latest
  call opened with any request that reports one in `open_workflow`, such as `OpenWorkflowRequest`,
  including for a workflow that's already open. Once that workflow closes, the session's next call
  fails with `WorkflowNotOpenResultFailure` instead of moving to another open workflow.
  [#6234](https://github.com/griptape-ai/griptape-nodes-engine/issues/6234)
