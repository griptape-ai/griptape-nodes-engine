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

## Library tools

Some node libraries provide their own MCP tools (see
[`get_mcp_tools`](../../development/custom_nodes/advanced_libraries.md#get_mcp_tools)). These
tools aren't served at the main address above. The editor's chat agent picks them up without
any setup (see [Library tools](../agent/mcp_servers.md#library-tools)).

To use one library's tools, add its address to your client as a separate server. The address
ends with the library's name in lowercase, with underscores in place of spaces and punctuation.
For a library named "Acme Render Tools":

```
http://localhost:8125/mcp/libraries/acme_render_tools/
```

Or, to get the engine's tools and some libraries' tools from one server, add the libraries to the
main address, separated by commas. For "Acme Render Tools" and "Paint & Composite Tools":

```
http://localhost:8125/mcp/?libraries=acme_render_tools,paint_composite_tools
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

### Name a workflow in the configuration

To point a client at one workflow, add its id as an `X-Griptape-Workflow-Id` header. See [Choose the workflow](#choose-the-workflow). In Claude Code:

```json
{
  "mcpServers": {
    "griptape-nodes": {
      "type": "streamable-http",
      "url": "http://localhost:8125/mcp/",
      "headers": { "X-Griptape-Workflow-Id": "<workflow id>" }
    }
  }
}
```

Cursor and VS Code take the same `headers` object beside `url`.

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

## Choose the workflow

Tool calls that build or change a workflow act on one open workflow. A client names it in one of two ways:

- **A header.** Send the workflow's id in an `X-Griptape-Workflow-Id` header. Every tool call acts on that workflow, including each request inside `EventRequestBatch`. A workflow keeps its id when it is saved. `ListOpenWorkflowsRequest` lists every open workflow with its id.
- **The session's own workflow.** A client that sends no header can open a workflow with `OpenWorkflowRequest`. Name an open workflow's `workflow_id` to work in it: it comes back with `already_open` set and nothing about it changes. Name none, with `create_flow` set to `true`, to start a new one. Later calls in the same MCP session act on that workflow. Any call that reports the workflow it opened in `open_workflow` does the same, such as `SetWorkflowContextRequest`, or `EnsureWorkflowAndFlowRequest` when it opens one or uses the only open one.

When the workflow a call acts on isn't open, such as after its tab was closed, the call fails with `WorkflowNotOpenResultFailure`. Its `open_workflows` lists the workflows that are open, so you can open one of them with `OpenWorkflowRequest`. A session's own workflow is forgotten after that failure. A call with neither a header nor a workflow of its session's own acts on no workflow, so a call that needs one fails, even while only one is open. The exception is `EnsureWorkflowAndFlowRequest`: it uses the only open workflow, and makes it the session's.

## Parameter values

Parameter and flow variable values arrive as plain JSON. Values of types JSON lacks arrive as
their closest JSON form: tuples and sets as lists, enums as their value, paths as text, and
artifacts and other objects as their fields:

```json
{"type": "ImageUrlArtifact", "value": "https://example.com/cat.png"}
```

Send values back as plain JSON too. Two kinds of dict arrive wrapped instead, and go back as they
arrived. A dict with its own `"$type"` key is wrapped so the key isn't read as a type. A dict
whose keys would collide as text, such as `1` and `"1"`, arrives as key-value pairs so no entry is
lost:

```json
{"$type": "builtins:dict", "$value": {"$type": "app.bsky.feed.post", "text": "hi"}}
{"$type": "builtins:dict", "$value": [[1, "a"], ["1", "b"]]}
```

A value that can't be written as JSON, such as an open file, is sent as its Python `str()` in
fields that only show it, for example
`"<_io.TextIOWrapper name='notes.txt' mode='r' encoding='UTF-8'>"`. Anywhere else the result
can't be sent, and the request gets a failure whose message names the value instead.

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
