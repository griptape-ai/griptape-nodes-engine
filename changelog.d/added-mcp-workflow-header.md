- MCP clients can now name the workflow their tool calls act on with an `X-Griptape-Workflow-Id`
  header. A client that sends none works, for the rest of its MCP session, on the workflow its own
  `EnsureWorkflowAndFlowRequest` or `SetWorkflowContextRequest` opened or selected. Either keeps
  reaching the workflow after saving a Griptape-provided template continues it in a copy. The
  editor's chat agent, including the tools libraries give it, acts on the workflow its message was
  sent from. See [Connect External MCP Clients](docs/guides/mcp/external_clients.md#choose-the-workflow).
