# Streamable HTTP Connection

The **streamable_http** connection type enables Griptape Nodes to communicate with MCP servers over HTTP with support for **bidirectional streaming** (client ↔ server) and real-time communication.

## When to Use Streamable HTTP

- **Bidirectional Communication**: When both client and server need to send data
- **Interactive Applications**: Real-time chat, collaborative editing, live collaboration
- **HTTP Infrastructure**: Leveraging existing HTTP-based systems
- **Custom Streaming**: When you need more control than SSE provides
- **Session Management**: Applications requiring persistent session state

## Available Streamable HTTP MCP Servers

- **[Exa](../servers/exa.md)** - Advanced web search and research capabilities

## Example Streamable HTTP MCP Server Configuration

### Chat Application Server

```json
{
  "name": "chat_app",
  "transport": "streamable_http",
  "url": "https://api.chat-service.com/mcp/stream",
  "headers": {
    "Authorization": "Bearer chat-token"
  },
  "timeout": 60,
  "sse_read_timeout": 120,
  "terminate_on_close": false,
  "description": "Real-time messaging and communication"
}
```

## Popular Streamable HTTP Use Cases

- **Chat Applications** - Real-time messaging and communication
- **Collaborative Editing** - Shared document editing (like Google Docs)
- **Live Collaboration** - Team workspaces and shared whiteboards
- **Interactive Dashboards** - Real-time data visualization and interaction
- **Customer Support** - Live chat and support systems
- **Online Gaming** - Turn-based and real-time multiplayer games

## Configuration

### Required Fields

| Field | Type   | Description                      | Example                         |
| ----- | ------ | -------------------------------- | ------------------------------- |
| `url` | string | HTTP endpoint for the MCP server | `"https://api.example.com/mcp"` |

### Optional Fields

| Field                | Type    | Description                     | Default |
| -------------------- | ------- | ------------------------------- | ------- |
| `headers`            | object  | HTTP headers for authentication | `{}`    |
| `timeout`            | number  | Request timeout in seconds      | `30`    |
| `sse_read_timeout`   | number  | SSE read timeout in seconds     | `60`    |
| `terminate_on_close` | boolean | Terminate session on close      | `true`  |

## Example Configurations

### Basic Streamable HTTP

```json
{
  "name": "streamable_api",
  "transport": "streamable_http",
  "url": "https://api.example.com/mcp/stream",
  "description": "HTTP API with bidirectional streaming (client ↔ server)"
}
```

### Authenticated Streamable HTTP

```json
{
  "name": "auth_streamable",
  "transport": "streamable_http",
  "url": "https://api.example.com/mcp/stream",
  "headers": {
    "Authorization": "Bearer your-token-here",
    "Content-Type": "application/json",
    "Accept": "application/json"
  },
  "timeout": 60,
  "sse_read_timeout": 120,
  "terminate_on_close": true
}
```

### Custom Configuration

```json
{
  "name": "custom_streamable",
  "transport": "streamable_http",
  "url": "https://mcp.example.com/stream",
  "headers": {
    "X-API-Key": "your-api-key",
    "X-Client-Version": "1.0.0",
    "User-Agent": "GriptapeNodes/1.0"
  },
  "timeout": 90,
  "sse_read_timeout": 300,
  "terminate_on_close": false
}
```

## Setup Steps

### 1. Deploy MCP Server

Ensure your MCP server supports streamable HTTP:

```python
# Example streamable HTTP endpoint
@app.post("/mcp/stream")
async def mcp_stream(request: Request):
    return StreamingResponse(
        process_mcp_stream(request),
        media_type="application/json",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
```

### 2. Configure in Griptape Nodes

1. Open Griptape Nodes settings
1. Navigate to MCP Server configuration
1. Add new server with streamable_http transport
1. Enter the server URL
1. Configure authentication and timeouts
1. Test the connection

### 3. Use in Workflow

1. Add MCPTask node to your flow
1. Select your configured streamable HTTP server
1. Enter your prompt
1. Execute the workflow

## Advantages

- **Bidirectional Streaming**: Full duplex communication (client ↔ server)
- **HTTP Compatible**: Works with standard web infrastructure
- **Real-Time Updates**: Live data streaming in both directions
- **Session Management**: Built-in session handling
- **Custom Implementation**: More control over streaming behavior
- **Interactive Applications**: Perfect for real-time collaboration

## Limitations

- **HTTP Overhead**: More overhead than direct connections
- **Network Dependent**: Requires stable network connection
- **Complexity**: More complex than simple HTTP requests
- **Resource Usage**: Higher resource consumption
- **Custom Implementation**: Requires more development work than SSE

## Streamable HTTP vs SSE

| Feature            | Streamable HTTP                         | SSE                                   |
| ------------------ | --------------------------------------- | ------------------------------------- |
| **Direction**      | Bidirectional (client ↔ server)         | Unidirectional (server → client)      |
| **Protocol**       | Custom HTTP streaming                   | Standardized (`text/event-stream`)    |
| **Use Case**       | Interactive apps, real-time chat        | Notifications, live feeds, monitoring |
| **Implementation** | Custom client/server logic              | Built-in browser support              |
| **Reconnection**   | Manual implementation                   | Automatic reconnection                |
| **Example**        | Chat application, collaborative editing | Stock ticker, news feed               |

## Authentication

### Bearer Token

```json
{
  "headers": {
    "Authorization": "Bearer your-jwt-token"
  }
}
```

### API Key

```json
{
  "headers": {
    "X-API-Key": "your-api-key",
    "X-Client-ID": "griptape-nodes"
  }
}
```

### Custom Authentication

```json
{
  "headers": {
    "X-Custom-Auth": "your-custom-token",
    "X-User-ID": "user123",
    "X-Session-ID": "session456"
  }
}
```

## Session Management

### Terminate on Close

```json
{
  "terminate_on_close": true
}
```

- Automatically terminates the session when connection closes
- Useful for stateless operations
- Default behavior

### Persistent Sessions

```json
{
  "terminate_on_close": false
}
```

- Maintains session state across connections
- Useful for stateful operations
- Requires server-side session management

## Troubleshooting

### Connection Issues

- Verify server URL is accessible
- Check network connectivity
- Test with curl or Postman
- Monitor server logs

### Timeout Problems

- Increase timeout values
- Check server response times
- Monitor network latency
- Optimize server performance

### Authentication Failures

- Verify credentials are correct
- Check token expiration
- Ensure proper header format
- Test authentication separately

### Streaming Issues

- Verify server supports streaming
- Check for proper content types
- Monitor connection stability
- Test with smaller payloads

## Best Practices

1. **Use HTTPS**: Always use secure connections
1. **Handle Reconnection**: Implement automatic reconnection
1. **Monitor Sessions**: Track session state and cleanup
1. **Optimize Timeouts**: Set appropriate timeout values
1. **Secure Credentials**: Store sensitive data securely

## Example Use Cases

### Interactive Chat

```json
{
  "name": "chat_interactive",
  "transport": "streamable_http",
  "url": "https://chat.example.com/stream",
  "headers": {
    "Authorization": "Bearer chat-token"
  },
  "terminate_on_close": false
}
```

### Real-Time Collaboration

```json
{
  "name": "collaboration",
  "transport": "streamable_http",
  "url": "https://collab.example.com/stream",
  "headers": {
    "X-User-ID": "user123",
    "X-Workspace-ID": "workspace456"
  }
}
```

### Live Data Processing

```json
{
  "name": "data_processor",
  "transport": "streamable_http",
  "url": "https://processor.example.com/stream",
  "timeout": 120,
  "sse_read_timeout": 600
}
```

## Performance Considerations

### Timeout Configuration

- **Short Timeouts**: For quick operations (30-60 seconds)
- **Medium Timeouts**: For standard operations (60-120 seconds)
- **Long Timeouts**: For complex operations (120+ seconds)

### Connection Pooling

- Reuse connections when possible
- Monitor connection limits
- Implement proper cleanup
- Handle connection failures gracefully

## Next Steps

- [WebSocket Connection](./websocket.md) - Full-duplex communication
- [SSE Connection](./sse.md) - Unidirectional streaming
- [stdio Connection](./stdio.md) - Local process communication
