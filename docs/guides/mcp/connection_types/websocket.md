# WebSocket Connection

The **websocket** connection type enables Griptape Nodes to communicate with MCP servers using WebSocket protocol for full-duplex, real-time communication.

> **Note**: WebSocket is not an official MCP transport type, but it can be implemented as a custom transport according to the [MCP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

## What WebSocket Is

WebSocket is a communication protocol that provides full-duplex communication channels over a single TCP connection. Unlike HTTP, WebSocket allows both the client and server to send data at any time.

## Coming Soon

WebSocket support for MCP servers is still emerging. While the connection type can be implemented as a custom transport, there are currently no widely available WebSocket-based MCP servers to demonstrate with.

**For now, consider:**

- **[Streamable HTTP](./streamable_http.md)** for HTTP-based communication with real examples
- **[Local Process (stdio)](./stdio.md)** for local server connections with many examples

## Configuration

When WebSocket MCP servers become available, they will use a configuration similar to:

```json
{
  "transport": "websocket",
  "url": "ws://your-websocket-server.com/mcp",
  "headers": {
    "Authorization": "Bearer your-api-key"
  }
}
```

## Next Steps

- **[Streamable HTTP](./streamable_http.md)** - HTTP-based communication with real examples
- **[Local Process (stdio)](./stdio.md)** - Local server connections with many examples
- **[Example MCP Servers](../servers/index.md)** - Available server examples
