# Connect External MCP Clients to Griptape Nodes

Griptape Nodes runs its own MCP server so external agents (Claude Desktop, Claude Code, Cursor, VS Code, etc.) can drive the engine. This page is the inverse of the rest of this section: instead of Griptape Nodes consuming external MCP servers, here we expose Griptape Nodes itself as an MCP server.

## URL

By default the engine listens on:

```
http://localhost:8125/mcp/
```

The transport is **Streamable HTTP**. The trailing slash is recommended; the server redirects `/mcp` to `/mcp/` for clients that drop it.

When the engine starts, it logs the actual bound address, for example:

```
INFO MCP server listening at http://127.0.0.1:8125/mcp/
```

## Overrides

The host and port are controlled by environment variables:

| Variable                   | Default     | Description                                                              |
| -------------------------- | ----------- | ------------------------------------------------------------------------ |
| `GTN_MCP_SERVER_HOST`      | `localhost` | Interface to bind. Use `127.0.0.1` to be explicit, or `0.0.0.0` for LAN. |
| `GTN_MCP_SERVER_PORT`      | `8125`      | TCP port. Set to `0` to let the OS assign a free port.                   |
| `GTN_MCP_SERVER_LOG_LEVEL` | `ERROR`     | uvicorn log level for the MCP server.                                    |

If the configured port is already in use, the engine falls back to an OS-assigned port. Check the startup log to see the actual URL.

!!! warning "Local-only by default"

    The engine binds to `localhost`, which means only processes on the same machine can reach it. The MCP server has no authentication. Do not bind to `0.0.0.0` or expose the port to the network unless you fully trust everything that can reach it.

## Client configuration

### Claude Code

Add to `~/.claude.json` (or use `claude mcp add`):

```json
{
  "mcpServers": {
    "griptape-nodes": {
      "type": "streamable-http",
      "url": "http://localhost:8125/mcp/"
    }
  }
}
```

### Cursor

Create `~/.cursor/mcp.json` for global access, or `.cursor/mcp.json` in a workspace:

```json
{
  "mcpServers": {
    "griptape-nodes": {
      "url": "http://localhost:8125/mcp/"
    }
  }
}
```

### VS Code

Create `.vscode/mcp.json` in your workspace, or open the user file via **MCP: Open User Configuration**:

```json
{
  "servers": {
    "griptape-nodes": {
      "type": "http",
      "url": "http://localhost:8125/mcp/"
    }
  }
}
```

Note that VS Code uses `servers` (not `mcpServers`) and `"type": "http"`.

### Claude Desktop

Claude Desktop's `claude_desktop_config.json` only supports `stdio` servers. To connect to a remote/HTTP MCP server, either:

- Use **Settings → Connectors → Add custom connector** in the app and paste `http://localhost:8125/mcp/`, or
- Wrap the URL with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) in the config file:

```json
{
  "mcpServers": {
    "griptape-nodes": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8125/mcp/"]
    }
  }
}
```

## Verifying the connection

The simplest non-interactive check is the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) CLI. The engine speaks Streamable HTTP, so pass `--transport http`:

```bash
npx -y @modelcontextprotocol/inspector --cli http://localhost:8125/mcp/ \
  --transport http --method tools/list
```

Without `--transport http` the inspector defaults to SSE and you'll see `SSE error: Non-200 status code (400)`, which is the engine refusing an SSE-style GET without an MCP session.

The inspector also has a browser UI:

```bash
npx -y @modelcontextprotocol/inspector
```

Paste `http://localhost:8125/mcp/` into the URL field and pick **Streamable HTTP** as the transport. If the connection fails with `TypeError: NetworkError when attempting to fetch resource`, it's almost always CORS: the engine's MCP server does not currently emit `Access-Control-Allow-Origin` headers, so cross-origin browser fetches are blocked. Use the CLI command above instead, or run the inspector with browser security relaxed.

## Install the workflow-construction skill

The engine ships a [`griptape-nodes-workflows` skill](https://docs.griptapenodes.com/en/stable/skills/griptape-nodes-workflows/SKILL/) that teaches an agent how to drive the MCP tools described above (cold-start recipe, `EventRequestBatch`, common gotchas). Claude Code, Cursor, and VS Code natively load skills with the `name` + `description` frontmatter convention from [agentskills.io](https://agentskills.io), so installation is a directory drop.

The published markdown lives at:

```
https://docs.griptapenodes.com/en/stable/skills/griptape-nodes-workflows/SKILL/index.md
```

Whichever scope you choose, the directory name **must** be `griptape-nodes-workflows` (it has to match the `name` field in the frontmatter) and the file **must** be named `SKILL.md`.

### Per-client install paths

| Client            | Project scope                                      | User scope                                            |
| ----------------- | -------------------------------------------------- | ----------------------------------------------------- |
| Claude Code       | `.claude/skills/griptape-nodes-workflows/SKILL.md` | `~/.claude/skills/griptape-nodes-workflows/SKILL.md`  |
| Cursor            | `.cursor/skills/griptape-nodes-workflows/SKILL.md` | `~/.cursor/skills/griptape-nodes-workflows/SKILL.md`  |
| VS Code (Copilot) | `.github/skills/griptape-nodes-workflows/SKILL.md` | `~/.copilot/skills/griptape-nodes-workflows/SKILL.md` |

Cursor and VS Code also pick up `.agents/skills/` (project) and `~/.agents/skills/` (user), and VS Code additionally recognizes `.claude/skills/` / `~/.claude/skills/`. If you want one folder to serve multiple clients, drop the skill at `~/.agents/skills/griptape-nodes-workflows/SKILL.md`.

### Install in one command

Adjust `DEST` per the table above:

```bash
DEST="$HOME/.claude/skills/griptape-nodes-workflows"
mkdir -p "$DEST" \
  && curl -fsSL https://docs.griptapenodes.com/en/stable/skills/griptape-nodes-workflows/SKILL/index.md \
       -o "$DEST/SKILL.md"
```

Confirm it loaded by typing `/skills` in chat (Claude Code or VS Code) or opening the Skills tab in the customization menu (Cursor).
