# Connect Your Agent to the World

Think of MCP (Model Context Protocol) as a universal translator that lets your Agents talk to various apps and services. Instead of your Agent being stuck in its own little world, MCP lets it reach out and use real tools, access real data, and work with real applications.

## What Can Your Agent Do with MCP?

Using MCP, Agents can now:

- **Work with your files** - Read, write, and organize documents on your computer
- **Use professional software** - Control 3D modeling tools like Blender and Maya
- **Search the web** - Find information using search engines like Brave
- **Send messages** - Post updates to Slack channels and communicate with your team
- **Manage databases** - Store and retrieve information from databases
- **Remember things** - Keep track of important information between conversations

## Why This Matters

Before MCP, your Agent was like a smart assistant locked in a room with no windows. Now it's like giving that assistant keys to the entire building - it can go anywhere, use any tool, and help you with real work.

**Real examples:**

- Ask your Agent to "Rename all the models in my Blender scene to .." and it will actually connect to Blender and rename them
- Tell it to "Find the latest news about AI" and it will search the web and bring back real results
- Say "Send a project update to the team" and it will post to your Slack channel
- Request "Organize my documents" and it will actually move and sort your files

## Connection Types

There are two official ways your Agent can connect to other applications:

1. **[Local Apps](./connection_types/stdio.md)** - Connect to software on your computer

    **Examples**: [Fetch](./servers/fetch.md) (web content), [Filesystem](./servers/filesystem.md) (your files), [Time](./servers/time.md) (dates and schedules)

    **Best for**: Software you have installed on your computer

1. **[Web Services](./connection_types/streamable_http.md)** - Connect to web-based applications and services

    **Examples**: [Exa](./servers/exa.md) (web search), live data feeds, real-time updates

    **Best for**: Web-based applications, remote services, and real-time communication

### Additional Options

- **[Server-Sent Events (SSE)](./connection_types/sse.md)** - ⚠️ Deprecated (now part of Streamable HTTP)
- **[WebSocket](./connection_types/websocket.md)** - Custom transport (may be implemented by clients/servers)

## Ready to Get Started?

1. **[Getting Started Tutorial](./getting_started.md)** - Step-by-step tutorial using the Fetch MCP server
1. **[Using MCPTask with Agents](./mcp_task_agents.md)** - How to make your AI agents use these tools
1. **[MCP Server Rules](./rules.md)** - Guide agents with custom rules for each MCP server
1. **[Local Models with Agents](./advanced_local_models.md)** - Use local AI models for sensitive data processing
1. **[Example MCP Servers](./servers/index.md)** - Setup guides for some example servers

## Need More Information?

- [Official MCP Documentation](https://modelcontextprotocol.io/docs/getting-started/intro) - Learn more about the technology behind MCP
- [MCP Servers Repository](https://github.com/modelcontextprotocol/servers) - Find more apps you can connect
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-06-18) - Technical details for developers

## Ready to Connect Your First App?

Start with our [Getting Started Guide](./getting_started.md) - it will walk you through connecting your first app in just a few minutes!
