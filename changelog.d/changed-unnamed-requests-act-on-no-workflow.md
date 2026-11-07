- A request that leaves `open_workflow_id` out now acts on no workflow, so one that needs a workflow
  fails with `WorkflowNotNamedResultFailure`, which lists the open ones. Saved workflow files, node
  libraries, transport clients such as `gtn request`, and `EnsureWorkflowAndFlowRequest` keep using
  the only open workflow; MCP clients name theirs with the `X-Griptape-Workflow-Id` header or by
  opening one.
  [#6235](https://github.com/griptape-ai/griptape-nodes-engine/issues/6235)
