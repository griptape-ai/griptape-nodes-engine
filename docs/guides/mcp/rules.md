# MCP Server Rules

MCP server rules allow you to provide custom instructions to AI agents when they use tools from a specific MCP server. These rules are automatically applied as a ruleset whenever an agent uses that server's tools, helping ensure consistent and appropriate behavior.

## What Are Rules?

Rules are text instructions that guide how the AI agent should interact with a particular MCP server. They're added to the MCP server configuration and automatically applied when:

- Using the **MCPTask** node with that server
- Using the **Agent** node with that server configured

## Why Use Rules?

Rules help you:

- **Guide Agent Behavior**: Provide specific instructions on how to use the server's tools
- **Ensure Consistency**: Make sure the agent follows your preferred patterns
- **Handle Edge Cases**: Instruct the agent on how to handle errors or special situations
- **Optimize Usage**: Guide the agent to use the server's capabilities most effectively

## Adding Rules to an MCP Server

### When Creating a Server

When creating a new MCP server, you can add rules in the **Rules** text area:

1. Go to **Settings** → **MCP Servers**
1. Click **+ New MCP Server**
1. Fill in the server configuration (name, connection type, etc.)
1. In the **Rules** text area, enter your custom rules
1. Click **Create Server**

### When Editing a Server

1. Go to **Settings** → **MCP Servers**
1. Click the **Edit** button on the server you want to modify
1. Modify the text in the **Rules** text area
1. Save your changes

## Example Rules

### For a Web Fetching Server

```
Always validate URLs before fetching. Check that URLs use HTTPS when possible. If a fetch fails, return a clear error message explaining what went wrong.
```

### For a File System Server

```
Always check if a file exists before attempting to read it. Use absolute paths when possible. Never delete files without explicit user confirmation.
```

### For a Search Server

```
Always verify search results are relevant before returning them. If no relevant results are found, suggest alternative search terms. Format results in a clear, readable structure.
```

### For a Database Server

```
Always validate SQL queries before executing them. Never execute DROP or DELETE operations without explicit confirmation. Return query results in a structured format.
```

## How Rules Work

1. **Storage**: Rules are stored as part of the MCP server configuration
1. **Application**: When an agent uses tools from that server, the rules are automatically added as a ruleset
1. **Scope**: Rules apply only when using that specific MCP server
1. **Format**: Rules are a single string - you can include multiple instructions separated by periods or newlines

## Best Practices

### Be Specific

✅ **Good**: "Always validate URLs before fetching. Check for HTTPS and return clear error messages."

❌ **Vague**: "Be careful with URLs."

### Focus on Behavior

✅ **Good**: "Return errors in JSON format with 'error' and 'message' fields."

❌ **Too Generic**: "Handle errors well."

### Keep It Concise

✅ **Good**: "Validate inputs before processing. Return structured JSON responses."

❌ **Too Long**: A paragraph explaining every possible scenario in detail.

### Test Your Rules

After adding rules, test them with your MCP server to ensure they work as expected:

1. Create an MCPTask node using the server
1. Run a test prompt
1. Verify the agent follows the rules you specified

## Rules in Different Contexts

### MCPTask Node

When using the MCPTask node, rules from the selected MCP server are automatically applied to the agent that processes the task.

### Agent Node

When using the Agent node with MCP servers configured, rules from all enabled MCP servers are collected and applied to the agent.

## Troubleshooting

### Rules Not Being Applied

- Verify the rules are entered in the Rules text area (not empty)
- Check that the server is enabled
- Ensure you're using the correct server name in your workflow

### Agent Not Following Rules

- Make rules more specific and actionable
- Test with simpler rules first
- Check that the rules are appropriate for the server's capabilities

## Next Steps

- **[Getting Started Tutorial](./getting_started.md)** - Learn how to set up your first MCP server
- **[Connection Types](./index.md#connection-types)** - Learn about different connection methods
- **[Example Servers](./servers/index.md)** - See examples of configured MCP servers
