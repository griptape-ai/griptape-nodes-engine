# Coding Agent CLIs

The [Agent](../index.md) can run on a coding agent you already have installed: Claude Code. Griptape Nodes starts the CLI for each message, connects it to your project through the Griptape Nodes MCP server, and shows its replies and tool calls in the chat like any other provider.

The CLI uses its own sign-in, models, and settings. Usage is billed by the CLI's provider, not by Griptape Cloud. Griptape Nodes doesn't pass `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` to Claude Code, so it bills your Claude sign-in even when a key is set for your Anthropic nodes.

## Supported CLIs

| Provider    | Command  | Install                                                                                                     |
| ----------- | -------- | ----------------------------------------------------------------------------------------------------------- |
| Claude Code | `claude` | [claude.com/product/claude-code](https://claude.com/product/claude-code), then run `claude` once to sign in |

Sign in once in a terminal before adding the provider. Griptape Nodes can't answer a CLI's sign-in prompts for you.

## Add the Provider

1. Open **Settings → Agent Settings**
1. Click **+ Add Provider**
1. Select **Claude Code**

- **Name**: leave as is or enter a custom label
- **Base URL**: leave empty. The CLI connects to its own service.
- **Model (optional)**: a model name the CLI accepts, such as `sonnet` for Claude Code. Leave it blank to use the CLI's default.

Click **Create Provider**.

## How It Differs From Other Providers

- **Tools run without asking.** The CLI runs every tool, including running workflows and nodes, without showing [approval cards](../approvals.md). It is started in the mode that skips its own permission prompts too, so it can also run shell commands and edit files in your workspace.
- **It works in your workspace.** The CLI runs in your workspace directory, so it reads that folder's instruction files (such as `CLAUDE.md`) and its own project settings, as it would in a terminal.
- **Its own tools and skills.** The CLI keeps its built-in tools, skills, and MCP servers. Image generation through Griptape Cloud is not available.
- **Its own memory.** Each thread keeps the CLI's own session, so the CLI remembers the whole conversation. If you switch providers partway through a thread, the next provider is told what was said before it joined.
- **Sidebar only.** These providers run the chat Agent. The **Agent node** in workflows can't use them.

## When the CLI Isn't Found

If the Agent reports that a command was not found, the engine can't see the CLI on its `PATH`. This happens most often when the engine is started by the desktop app. Set the full path to the CLI in your config file under `agent.cli_commands`, keyed by provider type:

```json
{
  "agent": {
    "cli_commands": {
      "claude_code": "/Users/me/.local/bin/claude"
    }
  }
}
```

The provider type is `claude_code`. Run `which claude` (or `where claude` on Windows) in a terminal to find the path.

## Related

- [AI Providers overview](./index.md)
- [MCP Servers](../mcp_servers.md): servers you attach to a thread are passed to the CLI too
