# MCP Servers

The **MCP servers** dropdown lets you attach one or more MCP servers to the agent for a conversation. This gives the agent access to external tools — files, web search, Blender, Maya, and more — depending on which servers you have configured.

<!-- TODO(#5095): screenshot of the MCP servers dropdown -->

See [MCP Integration](../mcp/index.md) for how to set up MCP servers.

## Editing a server mid-conversation

Changes you make to an MCP server take effect on your **next message** — you don't have to restart the engine or start a new conversation. This covers everything on the server: its command, arguments, environment variables, working directory, URL, headers, and its **Rules**.

Send another message after saving and the agent uses the updated server. Your conversation history is kept, so you can simply ask again.

A server you didn't change keeps its existing connection, so editing one server won't slow down or interrupt the others. The server you edited reconnects on the next message, which can add a moment before the agent's first tool call.

If a server is switched off or deleted, the agent stops using it from your next message onward, and Griptape Nodes shuts down the connection to it.
