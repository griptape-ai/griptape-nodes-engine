- `CloseWorkflowRequest` closes the open workflow the request envelope names and leaves any others
  open. MCP clients get it as a tool. `ClearAllObjectStateRequest` always closes every open
  workflow, whatever the envelope or an MCP workflow header names.
  [#6205](https://github.com/griptape-ai/griptape-nodes-engine/issues/6205)
