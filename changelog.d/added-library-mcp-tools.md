- Advanced libraries can offer their own request types as tools on the engine's MCP server by
  returning them from `get_mcp_tools()`. Each library's tools are served at
  `/mcp/libraries/<library>/`, or alongside the engine's tools at `/mcp/?libraries=<library>`, so
  agents only load the tools of the products they use. See
  [Connect External MCP Clients](docs/guides/mcp/external_clients.md#library-tools).
