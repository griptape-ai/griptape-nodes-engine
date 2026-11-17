# Approvals

Some things the agent can do cost credits or change your project: running a workflow, running a node, generating an image, or using a tool from one of your own MCP servers. The agent stops and asks you before it does any of these, so a question like "how does this node work?" never turns into a workflow run you didn't ask for.

## What the agent asks about

The agent asks before it:

- runs your workflow, a flow, or a single node
- generates an image
- uses any tool from an MCP server you attached to the conversation (see [MCP Servers](./mcp_servers.md))
- uses a tool from a library you picked under **Libraries**, unless the library marks the tool as
    only reading information

Everything else runs without asking. That includes reading your canvas, listing libraries and nodes, creating and connecting nodes, setting parameter values, and saving the workflow.

## Answering an approval card

When the agent wants to do one of these things, its reply pauses and an approval card appears in the thread. The card shows the tool the agent wants to use and what it will send. Pick one:

- **Approve once** runs it this time. The agent asks again the next time it wants to do the same thing.
- **Approve for conversation** runs it, and lets the agent do the same thing again in this thread without asking. When the agent asked about several things at once, approving one for the conversation also approves the others it covers.
- **Deny** stops it. The agent is told you declined, and that it should not retry or find another way to do the same thing. It answers in text instead, or asks you first.

The reply carries on as soon as every card in it is answered.

If you send a new message instead of answering, every waiting card is treated as **Deny**, and the agent responds to your new message.

Answer a card while its reply is on screen. If you reload the editor, the card is not shown again; send a new message and the agent picks up from there.

## How long "Approve for conversation" lasts

An **Approve for conversation** choice applies only to the thread you made it in. A new thread starts with nothing approved, so the agent asks again.

While a thread has approvals, an **Allowed in this chat** line under the model picker lists them. Click **Reset** on that line to take them all back; the agent asks again from then on.

When the agent sends several actions together in one step, approving that step for the conversation approves each of the actions in it on its own. For example, approving a step that starts a flow means later requests to start a flow in the same thread run without asking, whether or not they come bundled with other actions.
