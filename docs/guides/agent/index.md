# Agent

The Agent is a built-in AI assistant with direct access to your workflow. Ask it questions, have it build nodes, run your workflow, or inspect what's on the canvas — all in plain conversation.

<!-- TODO(#5095): screenshot of the full sidebar showing the chat tab active, the model dropdown, and a sample conversation -->

## What it can do

The agent has built-in access to the Griptape Nodes engine via Griptape's built-in MCP server, so it can work with your workflow directly — not just talk about it. For example, you can ask it to:

- **"What nodes are in my workflow?"** — it can inspect the canvas and describe what's there
- **"Add a Text Input node and connect it to the Agent"** — it can create and wire nodes for you
- **"Run my workflow and tell me what the output was"** — it can execute your workflow and report back
- **"Change the prompt on my Agent node to..."** — it can update node parameters
- **"Explain what this workflow does"** — it can read your workflow structure and summarize it

This is powered by Griptape's built-in MCP server, which runs alongside the engine and exposes your workflow to the agent. You don't need to set anything up — it's always available.

## Opening the Sidebar

The sidebar lives on the right side of the Griptape Nodes canvas. Click the **chat bubble** tab (the first icon in the Sidebar Panels header) to open it.

<!-- TODO(#5095): screenshot highlighting the Sidebar Panels tabs, with the chat tab indicated -->
