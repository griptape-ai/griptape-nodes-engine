# Filesystem MCP Server

The **[Filesystem MCP Server](https://github.com/modelcontextprotocol/servers/blob/main/src/filesystem/README.md)** enables AI agents to perform file and directory operations on your local machine. It provides secure, controlled access to specific directories for file management, content organization, and data processing tasks.

## Installation

1. **Open Griptape Nodes** and go to **Settings** → **MCP Servers**

1. **Click + New MCP Server**

1. **Configure the server**:

    - **Server Name/ID**: `filesystem`
    - **Connection Type**: `Local Process (stdio)`
    - **Configuration JSON**:

    ```json
    {
    "transport": "stdio",
    "command": "npx",
    "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/path/to/allowed/directory"],
    "env": {},
    "encoding": "utf-8",
    "encoding_error_handler": "strict"
    }
    ```

1. **Click Create Server**

## Available Tools

- **`read_file`** - Read file contents
- **`write_file`** - Write content to files
- **`list_directory`** - List directory contents
- **`create_directory`** - Create new directories
- **`search_files`** - Find files by name or pattern
- **`move_file`** - Move or rename files
- **`delete_file`** - Remove files

## Configuration Options

To allow access to multiple directories, add them as separate arguments:

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": [
    "-y",
    "@modelcontextprotocol/server-filesystem",
    "/Users/username/Desktop",
    "/Users/username/Downloads",
    "/Users/username/Documents"
  ],
  "env": {},
  "encoding": "utf-8",
  "encoding_error_handler": "strict"
}
```

## Resources

- [Filesystem MCP Server](https://github.com/modelcontextprotocol/servers/blob/main/src/filesystem/README.md) - Official repository and documentation
- [Node.js File System API](https://nodejs.org/api/fs.html) - Reference for file operations
