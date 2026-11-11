- A request that names no workflow now acts on none when it comes from an MCP session without a
  workflow, from another engine, or straight to an `Engine`. Requests that need one, now including
  `CancelAllRunsRequest`, `ListRunsRequest` and `GetWorkflowContextRequest`, fail with
  `WorkflowNotNamedResultFailure` instead of answering as if the workflow were empty. The editor,
  `gtn request`, `EnsureWorkflowAndFlowRequest` and `GriptapeNodes.handle_request` still use the
  only open workflow, and a saved workflow file uses the one it opens. Node library code that reads
  the current workflow or flow through a manager, such as `GriptapeNodes.ContextManager()`, from a
  thread it started itself now finds none; start the thread with `asyncio.to_thread` so it keeps
  the node's workflow.
  [#6235](https://github.com/griptape-ai/griptape-nodes-engine/issues/6235)
