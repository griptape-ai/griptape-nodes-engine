- **Breaking:** The engine's MCP server and the chat agent no longer offer `ExecuteNodeRequest` as a
  tool. To run a single node, send `ResolveNodeRequest`, which runs node groups, loops and Workflow
  nodes the way a flow run does. `ExecuteNodeRequest` ran them without that handling.
