# Exa MCP Server

The **[Exa MCP Server](https://github.com/exa-labs/exa-mcp-server)** provides powerful web search and research capabilities through Exa AI's advanced search engine. It offers both local and remote deployment options, with specialized tools for code search, web research, and content extraction.

## Installation

The easiest way to use Exa is through their hosted MCP server:

1. **Open Griptape Nodes** and go to **Settings** → **MCP Servers**

1. **Click + New MCP Server**

1. **Configure the server**:

    - **Server Name/ID**: `exa`
    - **Connection Type**: `Streamable HTTP`
    - **Configuration JSON**:

    ```json
    {
        "transport": "streamable_http",
        "url": "https://mcp.exa.ai/mcp",
        "headers": {},
        "timeout": 30,
        "sse_read_timeout": 300,
        "terminate_on_close": true
    }
    ```

1. **Click Create Server**

## Available Tools

- **`get_code_context_exa`** - Search billions of GitHub repos, docs, and Stack Overflow for relevant code examples
- **`web_search_exa`** - Real-time web searches with optimized results
- **`crawling`** - Extract content from specific URLs
- **`company_research`** - Comprehensive company information gathering
- **`linkedin_search`** - Search LinkedIn for companies and people
- **`deep_researcher_start`** - Start AI-powered research on complex topics
- **`deep_researcher_check`** - Get comprehensive research reports

## Configuration Options

You can enable specific tools by adding them to your configuration:

```json
{
  "transport": "streamable_http",
  "url": "https://mcp.exa.ai/mcp",
  "enabled_tools": ["get_code_context_exa", "web_search_exa"]
}
```

Available tool combinations:

- **For Developers**: `["get_code_context_exa", "web_search_exa"]`
- **For Researchers**: `["web_search_exa", "deep_researcher_start", "deep_researcher_check"]`
- **For Business Intelligence**: `["company_research", "linkedin_search", "web_search_exa"]`
- **All Tools**: `["get_code_context_exa", "web_search_exa", "company_research", "crawling", "linkedin_search", "deep_researcher_start", "deep_researcher_check"]`

## Troubleshooting

### Common Issues

- **Connection Issues**: Test the remote server URL `https://mcp.exa.ai/mcp`, check your internet connection, verify firewall settings allow HTTPS connections
- **Tool Not Available**: Ensure the tool is enabled in your configuration, check the tool name spelling, verify the tool is available in the current Exa service

### Debug Tips

1. Test with simple queries first
1. Check the Exa dashboard for usage and errors
1. Use the remote server for easier troubleshooting
1. Start with basic tools before using advanced features

## Resources

- [Exa MCP Server](https://mcp.exa.ai/mcp) - Official server endpoint
- [Exa AI](https://exa.ai) - Exa AI platform and documentation
