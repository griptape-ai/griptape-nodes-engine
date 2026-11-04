# Server-Sent Events (SSE) Connection

⚠️ **Deprecated** - SSE as a standalone transport has been deprecated in favor of **Streamable HTTP**.

## What Happened to SSE?

According to the [official MCP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), SSE as a standalone transport was deprecated in protocol version 2024-11-05 and replaced by **Streamable HTTP**.

## Current MCP Transport Options

The MCP specification now defines only **two standard transport mechanisms**:

1. **[stdio](./stdio.md)** - Communication over standard in and standard out
1. **[Streamable HTTP](./streamable_http.md)** - HTTP-based communication with optional SSE support

## What This Means

- **SSE functionality is still available** - but as part of Streamable HTTP, not as a standalone transport
- **Streamable HTTP can use SSE** - for server-to-client streaming when needed
- **No standalone SSE servers** - SSE is now integrated into the HTTP transport

## Migration Path

If you were planning to use SSE, consider:

- **[Streamable HTTP](./streamable_http.md)** - The modern replacement that includes SSE capabilities
- **[Local Process (stdio)](./stdio.md)** - For local server connections
- **[WebSocket](./websocket.md)** - For full-duplex real-time communication (custom transport)

## Next Steps

- **[Streamable HTTP](./streamable_http.md)** - The modern HTTP transport with SSE capabilities
- **[Local Process (stdio)](./stdio.md)** - Local server connections with many examples
- **[Example MCP Servers](../servers/index.md)** - Available server examples
