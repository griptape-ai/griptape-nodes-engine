- The editor's chat agent can now read library docs. `GetNodeDocumentationRequest`,
  `ListLibraryDocumentationRequest` and `GetLibraryDocumentationPageRequest` are tools on the
  engine's MCP server, and the agent's instructions tell it to read a node's docs when it is unsure
  how to use the node. A page read over MCP comes back in parts of 64,000 characters unless the
  caller sets `limit`.
