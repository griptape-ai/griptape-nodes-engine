# Agent Sidebar

The Agent Sidebar is a built-in chat interface that gives you a direct line to an AI assistant — one that already knows your workflow. Ask it questions, have it build nodes for you, inspect what's on the canvas, or run your workflow, all in plain conversation.

<!-- TODO: screenshot of the full sidebar showing the chat tab active, the model dropdown, and a sample conversation -->

## What it can do

The sidebar agent has built-in access to the Griptape Nodes engine, so it can work with your workflow directly — not just talk about it. For example, you can ask it to:

- **"What nodes are in my workflow?"** — it can inspect the canvas and describe what's there
- **"Add a Text Input node and connect it to the Agent"** — it can create and wire nodes for you
- **"Run my workflow and tell me what the output was"** — it can execute your workflow and report back
- **"Change the prompt on my Agent node to..."** — it can update node parameters
- **"Explain what this workflow does"** — it can read your workflow structure and summarize it

This is powered by Griptape's built-in MCP server, which runs alongside the engine and exposes your workflow to the agent. You don't need to set anything up — it's always available.

## Opening the Sidebar

The sidebar lives on the right side of the Griptape Nodes canvas. Click the **chat bubble** tab (the first icon in the Sidebar Panels header) to open it.

<!-- TODO: screenshot highlighting the Sidebar Panels tabs, with the chat tab indicated -->

## Threads

Each conversation is a **thread**. Threads are named automatically with the date and time they were created.

- Click **+ New** to start a fresh conversation
- Previous threads are listed above the message input

<!-- TODO: screenshot of the thread header showing the timestamp name and "+ New" button -->

### Where threads are stored

Threads are saved to your local filesystem and persist across sessions. Each thread is stored as two files:

| File                    | Contents                         |
| ----------------------- | -------------------------------- |
| `thread_{id}.json`      | Full message history             |
| `thread_{id}.meta.json` | Title, timestamps, message count |

The storage location follows the [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/) convention:

| Platform | Path                                                    |
| -------- | ------------------------------------------------------- |
| macOS    | `~/Library/Application Support/griptape_nodes/threads/` |
| Linux    | `~/.local/share/griptape_nodes/threads/`                |
| Windows  | `%LOCALAPPDATA%\griptape_nodes\threads\`                |

If a history file becomes corrupt, Griptape Nodes moves it aside automatically (renamed with a `.corrupt-<timestamp>` suffix) so your other threads are unaffected.

## Choosing a Model

At the top of the chat panel there are two dropdowns: **Model** and **MCP servers**.

Click the **Model** dropdown to see all available models, grouped by provider:

<!-- TODO: screenshot of the model dropdown open, showing Griptape Cloud models at top and an Ollama section below -->

- The top group contains **Griptape Cloud** models (Claude, GPT, Gemini, DeepSeek, Llama, and more)
- Any additional providers you've configured appear as their own labeled sections below
- Use **Search models...** at the top of the dropdown to filter by name
- Click **Manage providers...** at the bottom to open the [AI Providers](./ai_providers/index.md) settings

The active model name is shown in the dropdown button when the list is closed.

## MCP Servers

The **MCP servers** dropdown lets you attach one or more MCP servers to the sidebar agent for a conversation. This gives the agent access to external tools — files, web search, Blender, Maya, and more — depending on which servers you have configured.

See [MCP Integration](./mcp/index.md) for how to set up MCP servers.

<!-- TODO: screenshot of the MCP servers dropdown -->

## Sending Messages

Type in the **Write a message...** input at the bottom and press Enter or click the send button. You can also attach files using the paperclip icon.

The agent streams its response back in real time. If it uses a tool (e.g., reads a file via an MCP server), you'll see tool call and result events inline in the conversation.

<!-- TODO: screenshot of an in-progress response with a tool call visible -->

## Personalization

You can customize how the agent talks to you and what context it has. Open **Settings → Agent Settings** and scroll down to the **Personalization** section.

<!-- TODO: screenshot of the full Personalization section -->

| Field                       | What it does                                                                                                                 |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **Address me as**           | The name or nickname the agent uses when addressing you                                                                      |
| **Tone**                    | The communication style (e.g., Concise, Formal, Casual)                                                                      |
| **My role**                 | Your job title or area of expertise — gives the agent context about your background                                          |
| **About me**                | Free-text interests, background, or any context you want the agent to know                                                   |
| **Additional instructions** | Extra rules appended to every response — use this to set a persona, define output format, or add domain-specific constraints |

Together these fields build the agent's system prompt. The more context you provide, the more tailored the responses.

## Skills

Skills are markdown files that give the sidebar agent extra instructions or domain knowledge — loaded automatically at startup and reloaded on every run, so edits take effect without restarting the engine.

Griptape Nodes ships with a built-in skill for building and running workflows. You can add your own alongside it.

### Where to put skills

Create a `.agents/skills/` folder inside your workspace directory and drop skill files in there:

```
<workspace_directory>/
└── .agents/
    └── skills/
        ├── my-skill.md
        └── another-skill.md
```

The default workspace directory is `GriptapeNodes/` inside wherever you launched the engine. You can find the exact path in **Settings → File System → Workspace Directory**.

### Skill file format

Each skill is a markdown file with a YAML frontmatter block:

```markdown
---
name: my-skill-name
description: What this skill does and when the agent should use it.
---

# Skill title

Instructions, reference material, or domain knowledge the agent should apply
when this skill is relevant. Write in plain English — the agent reads this as
part of its context.
```

The `name` and `description` fields tell the agent what the skill is for and when to use it. The body can be as long or short as you need — code snippets, step-by-step instructions, reference tables, etc.

### Example: a house style guide

```markdown
---
name: writing-style
description: Apply our house style when drafting or editing text.
---

# Writing Style Guide

- Use sentence case for headings, not title case.
- Prefer active voice.
- Avoid jargon; explain technical terms on first use.
- Maximum sentence length: 25 words.
```

### Hot reload

!!! tip

    Skills are picked up automatically — no engine restart needed. Drop a new `.md` file into `.agents/skills/`, and the very next message you send will use it.

## Related

- [AI Providers](./ai_providers/index.md) — add Ollama, LM Studio, or a custom OpenAI-compatible endpoint
- [MCP Integration](./mcp/index.md) — give the agent access to external tools
- [Agent Skills documentation](https://agentskills.io/home) — full reference for the skills format and capabilities
