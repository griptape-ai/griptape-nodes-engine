# Using in Workflows

The **Agent node** is how you bring AI into a workflow. Unlike the chat sidebar — which is interactive and conversational — the Agent node runs as part of an automated sequence, taking inputs from other nodes and passing outputs downstream.

## Key parameters

- **provider** — the AI provider to use (matches the providers you configure in [Providers](./providers/index.md))
- **prompt model** — the specific model from that provider
- **rulesets** — connect a Ruleset node or TextInput to shape the agent's behavior; skills are not supported in workflows
- **tools** — capabilities to give the agent
- **output_schema** — optionally constrain the output to a specific JSON structure

## Full reference

See the [Agent node reference](../../nodes/agents/create_agent.md) for the complete parameter list, output schema examples, and common issues.
