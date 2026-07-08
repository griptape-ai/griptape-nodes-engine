# Agent

The Agent is a built-in AI assistant with direct access to your workflow. Ask it questions, have it build nodes, run your workflow, or inspect what's on the canvas — all in plain conversation.

<!-- TODO(#5095): screenshot of the full sidebar showing the chat tab active, the model dropdown, and a sample conversation -->

## Open the sidebar

The sidebar lives on the right side of the Griptape Nodes canvas. Click the **chat bubble** tab (the first icon in the Sidebar Panels header) to open it.

<!-- TODO(#5095): screenshot highlighting the Sidebar Panels tabs, with the chat tab indicated -->

## Choose a model

At the top of the chat panel, click the **Model** dropdown to see all available models, grouped by provider:

<!-- TODO(#5095): screenshot of the model dropdown open, showing Griptape Cloud models at top and an Ollama section below -->

- The top group contains **Griptape Cloud** models (Claude, GPT, Gemini, DeepSeek, Llama, and more) — available by default, no setup needed
- Any additional providers you've configured appear as their own labeled sections below
- Use **Search models...** to filter by name
- Click **Manage providers...** at the bottom to open [Providers](./providers/index.md)

## Send a message

Type in the **Write a message...** input at the bottom and press **Enter** or click the send button. You can also attach files using the paperclip icon.

The agent streams its response in real time. When it takes an action — like reading your canvas or running your workflow — you'll see the step appear inline so you always know what it's doing.

<!-- TODO(#5095): screenshot of an in-progress response with a tool call visible -->

For example, you can ask it to:

- **"What nodes are in my workflow?"** — it reads the canvas and tells you what's there
- **"Add a Text Input node and connect it to the Agent"** — it creates and wires the nodes for you
- **"Run my workflow and tell me what the output was"** — it executes your workflow and reports back
- **"Change the prompt on my Agent node to..."** — it updates node parameters directly
- **"Explain what this workflow does"** — it reads your workflow structure and gives you a plain-language summary

!!! tip "How does the agent control my workflow?"
    Griptape Nodes runs a built-in **MCP server** alongside the engine. MCP (Model Context Protocol) is an open standard that lets AI models call tools — in this case, tools for reading your canvas, creating nodes, running workflows, and more. The agent connects to this server automatically; there's nothing to configure.

## What's next

- [Threads](./threads.md) — how conversations are saved and where to find them
- [Personalization](./personalization.md) — customize the agent's tone and context
- [Skills](./skills.md) — give the agent extra domain knowledge
- [External Tools (MCP Servers)](./mcp_servers.md) — connect file access, web search, and other tools to the agent
- [Providers](./providers/index.md) — add Ollama, LM Studio, or a custom endpoint
- [Using in Workflows](./using_in_workflows.md) — the Agent node for automated flows
