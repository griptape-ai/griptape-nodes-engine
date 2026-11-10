- A request that names no workflow now acts on none when it comes from an MCP session without a
  workflow, from another engine, or straight to an `Engine`. Requests that need one, now including
  `CancelAllRunsRequest`, `ListRunsRequest` and `GetWorkflowContextRequest`, fail with
  `WorkflowNotNamedResultFailure` instead of answering as if the workflow were empty. The editor,
  `gtn request`, saved workflow files, `EnsureWorkflowAndFlowRequest` and the `GriptapeNodes`
  classmethods still use the only open workflow.
  [#6235](https://github.com/griptape-ai/griptape-nodes-engine/issues/6235)
