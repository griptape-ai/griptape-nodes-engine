# Local Process (stdio) Connection

The **stdio** connection type allows Griptape Nodes to communicate with MCP servers running as local processes using standard input/output streams.

## When to Use stdio

- **Local Applications**: Running MCP servers on the same machine
- **Command-Line Tools**: Interacting with CLI-based MCP servers
- **Development**: Testing and development scenarios
- **Simple Setup**: When you want minimal configuration overhead

## Example stdio MCP Servers

Here are a couple of examples. There are of course many more MCP Servers you can set up, but we wanted to show a few that can help you get started.

- **[Fetch](../servers/fetch.md)** - Web content fetching and processing
- **[Filesystem](../servers/filesystem.md)** - File and directory operations

## Configuration

### Required Fields

| Field     | Type   | Description                     | Example                                         |
| --------- | ------ | ------------------------------- | ----------------------------------------------- |
| `command` | string | Command to start the MCP server | `"npx"`, `"python"`, `"uvx"`                    |
| `args`    | array  | Arguments passed to the command | `["-y", "@modelcontextprotocol/server-memory"]` |

### Optional Fields

| Field                    | Type   | Description             | Default           |
| ------------------------ | ------ | ----------------------- | ----------------- |
| `env`                    | object | Environment variables   | `{}`              |
| `cwd`                    | string | Working directory       | Current directory |
| `encoding`               | string | Text encoding           | `"utf-8"`         |
| `encoding_error_handler` | string | Error handling strategy | `"strict"`        |

## Example Configurations

### Memory Server (Node.js)

```json
{
  "name": "memory",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-memory"],
  "description": "Persistent memory storage for conversations"
}
```

### Filesystem Server

```json
{
  "name": "filesystem",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"],
  "env": {
    "NODE_ENV": "production"
  },
  "cwd": "/home/user/projects"
}
```

## Setup Steps

### 1. Install MCP Server

```bash
# For Node.js servers
npm install -g @modelcontextprotocol/server-memory

# For Python servers
pip install mcp-server-git
# or
uvx mcp-server-git
```

### 2. Configure in Griptape Nodes

1. Open Griptape Nodes settings
1. Navigate to MCP Server configuration
1. Add new server with stdio transport
1. Fill in command and arguments
1. Save the connection

### 3. Use in Workflow

1. Add MCPTask node to your flow
1. Select your configured stdio server
1. Enter your prompt
1. Execute the workflow

## Advantages

- **Low Latency**: Direct process communication
- **Simple Setup**: Minimal configuration required
- **Local Control**: Full control over the server process
- **Resource Efficient**: No network overhead

## Limitations

- **Local Only**: Cannot connect to remote servers
- **Process Management**: Must handle server lifecycle
- **Platform Dependent**: Command syntax varies by OS
- **Single Connection**: One connection per server instance

## Troubleshooting

### Server Won't Start

If the error is `[Errno 2] No such file or directory: 'npx'` (or `'uvx'`, `'python'`), the engine
can't find that command on its PATH:

- **Desktop app**: update to v0.27.0 or later. Earlier versions don't inherit your login shell's
    PATH on macOS and Linux, so a command installed via Homebrew, nvm, or similar isn't visible to
    the engine.
- **Running the engine outside the desktop app** (terminal, service, container) without a
    login-shell PATH: either put the tool on PATH before starting the engine, or set `command` to
    its absolute path (find it with `which npx`) and set a complete `PATH` in `env`. Setting `PATH`
    in `env` *replaces* the inherited PATH rather than extending it, so include every directory your
    other tools need, not just the one for this command.

If the command starts but the server still fails, also check:

- File permissions
- That all dependencies are installed
- Whether the command works when run manually in a terminal

### Connection Timeout

- Check if server is responding to stdio
- Verify encoding settings
- Look for server error messages
- Check working directory permissions

### Permission Errors

- Ensure proper file system permissions
- Check if user has access to required directories
- Verify environment variable access

## Best Practices

1. **Use Absolute Paths**: Needed only if the engine's PATH doesn't include your command's
    directory, as described in [Server Won't Start](#server-wont-start) above
1. **Set Environment Variables**: For configuration and secrets
1. **Handle Errors Gracefully**: Implement proper error handling
1. **Monitor Resources**: Watch for memory leaks or high CPU usage
1. **Test Commands**: Verify server commands work before configuration

## Next Steps

- [SSE Connection](./sse.md) - HTTP-based streaming
- [Streamable HTTP](./streamable_http.md) - HTTP with streaming support
- [WebSocket Connection](./websocket.md) - Full-duplex communication
