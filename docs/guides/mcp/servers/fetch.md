# Fetch MCP Server

The **[Fetch MCP Server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)** provides web content fetching capabilities, allowing AI agents to retrieve and process content from web pages. It converts HTML to markdown for easier consumption and is perfect for research, content analysis, and web scraping tasks.

## Installation

1. **Open Griptape Nodes** and go to **Settings** → **MCP Servers**

1. **Click + New MCP Server**

1. **Configure the server**:

    - **Server Name/ID**: `fetch`
    - **Connection Type**: `Local Process (stdio)`
    - **Configuration JSON**:

    ```json
    {
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-fetch"],
    "env": {},
    "encoding": "utf-8",
    "encoding_error_handler": "strict"
    }
    ```

1. **Click Create Server**

## Available Tools

- **`fetch`** - Fetch content from a web URL and convert to markdown

## Configuration Options

You can customize the fetch server behavior with environment variables:

```json
{
  "transport": "stdio",
  "command": "uvx",
  "args": ["mcp-server-fetch"],
  "env": {
    "FETCH_TIMEOUT": "30000",
    "FETCH_USER_AGENT": "MyApp/1.0"
  },
  "encoding": "utf-8",
  "encoding_error_handler": "strict"
}
```

Available environment variables:

- **`FETCH_TIMEOUT`** - Request timeout in milliseconds (default: `30000`)
- **`FETCH_USER_AGENT`** - User agent string for requests (default: `mcp-server-fetch`)
- **`FETCH_MAX_SIZE`** - Maximum response size in bytes (default: `10485760` (10MB))

## Troubleshooting

### Common Issues

- **Server Not Responding**: Check your internet connection, verify the URL is accessible in a browser, try a different URL to test the server
- **Content Not Loading**: Some websites block automated requests, try adding a custom user agent, check if the site requires authentication
- **Timeout Errors**: Increase the `FETCH_TIMEOUT` value, try smaller, simpler pages first, check your network connection speed
- **Invalid URLs**: Ensure URLs include the protocol (http:// or https://), check for typos in the URL, verify the website is accessible

## Resources

- [Fetch MCP Server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) - Official repository and documentation
