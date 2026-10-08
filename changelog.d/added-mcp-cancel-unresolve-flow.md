- MCP clients can now call `CancelFlowRequest` to stop a running flow and `UnresolveFlowRequest` to
  reset a flow's nodes, so an agent can recover a stuck run without reloading the workflow.
  [#5754](https://github.com/griptape-ai/griptape-nodes-engine/issues/5754)
