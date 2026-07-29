# Time MCP Server

The [**Time MCP Server**](https://github.com/modelcontextprotocol/servers/tree/main/src/time) provides date and time operations for AI agents, enabling them to work with temporal data, scheduling, and time-based calculations. It's perfect for any workflow that needs to handle dates, times, timezones, or scheduling.

## Installation

1. **Open Griptape Nodes** and go to **Settings** → **MCP Servers**

1. **Click + New MCP Server**

1. **Configure the server**:

    - **Server Name/ID**: `time`
    - **Connection Type**: `Local Process (stdio)`
    - **Configuration JSON**:

    ```json
    {
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "env": {},
        "cwd": null,
        "encoding": "utf-8",
        "encoding_error_handler": "strict"
    }
    ```

1. **Click Create Server**

## Available Tools

- **`get_current_time`** - Get the current date and time
- **`parse_date`** - Parse date strings into structured format
- **`format_date`** - Format dates into different string formats
- **`add_time`** - Add time periods to dates
- **`subtract_time`** - Subtract time periods from dates
- **`compare_dates`** - Compare two dates
- **`get_timezone_info`** - Get information about timezones
- **`convert_timezone`** - Convert times between timezones

## Resources

- [Time MCP Server](https://github.com/modelcontextprotocol/servers/tree/main/src/time) - Official repository and documentation
- [JavaScript Date Object](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Date) - Reference for date operations
- [Timezone Database](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) - Complete list of timezones
