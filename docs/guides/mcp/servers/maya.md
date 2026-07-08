# Maya MCP Server

The **[Maya MCP Server](https://github.com/PatrickPalmer/MayaMCP)** enables AI agents to directly interact with and control Autodesk Maya through natural language using the Model Context Protocol (MCP). This integration allows for prompt-assisted 3D modeling, scene creation, and manipulation. This is a third-party server created by [Patrick Palmer](https://github.com/PatrickPalmer), and is not made by Autodesk Maya or Griptape.

!!! warning "Third-Party Server"

    This MCP server is not officially supported by Griptape or Autodesk Maya. Use at your own discretion and ensure you have proper backups of your work.

!!! note "Prerequisites"

    Before using the Maya MCP server, you must:

    1. Have [Autodesk Maya](https://www.autodesk.com/products/maya) 2023 or newer installed
    1. Install Python 3.10 or greater
    1. Download and set up the Maya MCP server from the [repository](https://github.com/PatrickPalmer/MayaMCP). See the [installation instructions](#1-download-and-setup-maya-mcp-server) for specific info.

## Installation

### 1. Download and Setup Maya MCP Server

1. Open up a terminal and navigate to the location on your machine where you'd like to download the repository. I would recommend placing the server in an easily discoverable location. For example, on my Mac I like to place it where I keep my GitHub repos: `$HOME/Documents/GitHub`.

    ```bash
    cd $HOME/Documents/GitHub
    ```

1. **Clone the repository**:

    ```bash
    git clone https://github.com/PatrickPalmer/MayaMCP.git
    cd MayaMCP
    ```

1. **Create a virtual environment**:

    ```bash
    python -m venv .venv
    ```

1. **Activate the virtual environment**:

    - **Windows**: `.venv\Scripts\activate.bat`
    - **Mac/Linux**: `source .venv\bin\activate`

1. **Install dependencies**:

    ```bash
    pip install -r requirements.txt
    ```

### 2. Open Maya and Enable Command Port

1. **Open Autodesk Maya**

1. **Enable the Command Port** - This is required for the MCP server to communicate with Maya. In Maya's Script Editor, run this Python code:

    ```python
    import maya.cmds as cmds

    def setup_maya_command_port(port=50007):
        """Setup Maya command port with error handling"""
        try:
            # First, try to close any existing command port on this port
            try:
                cmds.commandPort(name=f"localhost:{port}", close=True)
                print(f"Closed existing command port on localhost:{port}")
            except:
                # No existing port to close, that's fine
                pass

            # Enable the command port
            cmds.commandPort(name=f"localhost:{port}")
            print(f"Command Port successfully enabled on localhost:{port}")
            return True

        except Exception as e:
            print(f"Error setting up command port: {e}")
            return False

    # Run the setup
    if setup_maya_command_port(50007):
        print("Maya MCP server should now be able to connect!")
    else:
        print("Failed to setup command port. Check Maya's Command Port settings in Preferences.")
    ```

!!! warning "Command Port Required Every Session"

    You must enable the Command Port every time you start Maya. The Command Port setting is not persistent between Maya sessions.

!!! tip "Make It Easier: Save as Maya Script"

    To make this process easier, you can save the command port setup as a Maya script:

    1. **Save the following script as `enable_mcp_command_port.py`**:

        ```python
        import maya.cmds as cmds

        def enable_mcp_command_port(port=50007):
            """Setup Maya command port with error handling"""
            try:
                # First, try to close any existing command port on this port
                try:
                    cmds.commandPort(name=f"localhost:{port}", close=True)
                    print(f"Closed existing command port on localhost:{port}")
                except:
                    # No existing port to close, that's fine
                    pass

                # Enable the command port
                cmds.commandPort(name=f"localhost:{port}")
                print(f"Command Port successfully enabled on localhost:{port}")
                return True

            except Exception as e:
                print(f"Error setting up command port: {e}")
                return False
        ```

    1. **Test the script** in Maya's Script Editor:

        ```python
        import enable_mcp_command_port
        enable_mcp_command_port.enable_mcp_command_port()
        ```

    1. **Choose one of these options**:

        **Option A: Create a Shelf Button**

        - Drag the test code from step 2 to the shelf to create a button
        - Click the button whenever you need to enable the command port

        **Option B: Auto-Start with userSetup.py**

        - Find Maya's userScripts directory:
            - **Windows**: `%USERPROFILE%\Documents\maya\2025\scripts\`
            - **macOS**: `~/Library/Preferences/Autodesk/maya/2025/scripts/`
            - **Linux**: `~/maya/2025/scripts/`
        - Add this line to your existing `userSetup.py` file (or create one if it doesn't exist):
            ```python
            import enable_mcp_command_port
            enable_mcp_command_port.enable_mcp_command_port()
            ```
        - Restart Maya - the command port will be enabled automatically

!!! tip

    The Maya MCP server communicates with Maya through the Command Port. When the MCP server first attempts to communicate with Maya, you may get a popup within Maya asking for permission. Click **"Allow All"** to enable ongoing communication.

### 3. Configure Griptape Nodes

1. **Open Griptape Nodes** and go to **Settings** → **MCP Servers**

1. **Click + New MCP Server**

1. **Configure the server**:

    - **Server Name/ID**: `maya`
    - **Connection Type**: `Local Process (stdio)`
    - **Configuration JSON** (choose the appropriate example for your platform):

    **Windows**:

    ```json
    {
      "transport": "stdio",
      "command": "C:\\path\\to\\MayaMCP\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\MayaMCP\\src\\maya_mcp_server.py"
      ],
      "cwd": null,
      "encoding": "utf-8",
      "encoding_error_handler": "strict"
    }
    ```

    **macOS/Linux**:

    ```json
    {
      "transport": "stdio",
      "command": "/path/to/MayaMCP/.venv/bin/python",
      "args": [
        "/path/to/MayaMCP/src/maya_mcp_server.py"
      ],
      "cwd": null,
      "encoding": "utf-8",
      "encoding_error_handler": "strict"
    }
    ```

    !!! warning "Path Configuration"

        Replace the example paths with the actual absolute path to your MayaMCP project directory. Use forward slashes (`/`) on macOS/Linux and backslashes (`\\`) on Windows.

1. **Click Create Server**

## Available Tools

### Basic Tools

- **`list_objects_by_type`** - Get a list of objects in the scene, with optional filtering for cameras, lights, materials, or shapes
- **`create_object`** - Create basic objects (cube, cone, sphere, cylinder, camera, lights)
- **`get_object_attributes`** - Get a list of attributes on a Maya object
- **`set_object_attribute`** - Set an object's attribute with a specific value
- **`scene_new`** - Create a new scene in Maya
- **`scene_open`** - Load a scene into Maya
- **`scene_save`** - Save the current scene
- **`select_object`** - Select an object in the scene
- **`clear_selection_list`** - Clear the user selection list
- **`viewport_focus`** - Center and fit the viewport to focus on an object

### Advanced Modeling Tools

- **`create_advanced_model`** - Create complex 3D models (cars, trees, buildings, cups, chairs) with detailed parameters
- **`mesh_operations`** - Perform modeling operations (extrude, bevel, subdivide, boolean, combine, bridge, split)
- **`create_material`** - Create and assign materials (lambert, phong, wood, marble, chrome, glass, etc.)
- **`create_curve`** - Generate NURBS curves (line, circle, spiral, helix, star, gear, etc.)
- **`curve_modeling`** - Create geometry using curve-based modeling (extrude, loft, revolve, sweep, etc.)
- **`organize_objects`** - Organize objects through grouping, parenting, layout, alignment, and distribution

## Configuration Options

You can customize the Maya connection by modifying the configuration:

```json
{
  "transport": "stdio",
  "command": "/path/to/MayaMCP/.venv/bin/python",
  "args": [
    "/path/to/MayaMCP/src/maya_mcp_server.py"
  ],
  "cwd": "/path/to/MayaMCP",
  "encoding": "utf-8",
  "encoding_error_handler": "strict"
}
```

### Platform-Specific Paths

**Windows**:

```json
{
  "command": "C:\\path\\to\\MayaMCP\\.venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\MayaMCP\\src\\maya_mcp_server.py"]
}
```

**macOS/Linux**:

```json
{
  "command": "/path/to/MayaMCP/.venv/bin/python",
  "args": ["/path/to/MayaMCP/src/maya_mcp_server.py"]
}
```

## Example Use Cases

Here are some examples of what you can create with the Maya MCP server:

- "Create a simple car model with 4 wheels and a sporty design"
- "Build a tree with 3 branches and dense foliage"
- "Create a building with windows and apply a brick material"
- "Make a cup and apply a chrome material to it"
- "Create a chair and position it in the scene"
- "Generate a spiral curve and extrude it to create a spring"
- "Create a gear-shaped curve for mechanical modeling"
- "Group all the furniture objects together"
- "Align all objects to the world origin"
- "Create a new scene and save it as 'my_project.ma'"

## Advanced Features

### Custom Model Creation

The `create_advanced_model` tool supports various model types with specific parameters:

**Car Model**:

```json
{
  "model_type": "car",
  "parameters": {
    "wheels": 4,
    "sporty": true,
    "convertible": false
  }
}
```

**Tree Model**:

```json
{
  "model_type": "tree",
  "parameters": {
    "branches": 3,
    "leaf_density": 0.8,
    "type": "pine"
  }
}
```

### Material Creation

Create various material types with custom properties:

**Chrome Material**:

```json
{
  "material_type": "chrome",
  "color": [0.8, 0.8, 0.8],
  "parameters": {
    "reflectivity": 0.9
  }
}
```

**Wood Material**:

```json
{
  "material_type": "wood",
  "color": [0.6, 0.4, 0.2],
  "parameters": {
    "veinSpread": 0.5,
    "veinColor": [0.3, 0.2, 0.1]
  }
}
```

## Troubleshooting

### Common Issues

- **Connection Issues**: Ensure Maya is running and the Command Port is enabled, verify the MCP server is configured correctly in Griptape Nodes, check that the Python path points to the correct virtual environment
- **Permission Denied**: When Maya first connects, click "Allow All" in the Maya popup to enable communication
- **Path Issues**: Use absolute paths in the configuration, ensure the MayaMCP project path is correct
- **Python Version**: Ensure you're using Python 3.10 or greater

### Debug Tips

1. Test with simple commands first (e.g., "list all objects in the scene")
1. Verify Maya is running and accessible
1. Check that the virtual environment is properly activated
1. Ensure all file paths in the configuration are absolute and correct
1. Restart both Maya and the MCP connection if issues persist

### Maya Command Port

The Maya MCP server uses Maya's default Command Port for communication. This means:

- No additional Maya plugins or addons are required
- Communication happens through MEL scripting
- Python code is executed within Maya's Python interpreter
- Results are returned through the command port

## Security Considerations

!!! danger "Arbitrary Code Execution"

    The Maya MCP server executes Python code within Maya's environment. This can be powerful but potentially dangerous:

    - **Always save your Maya work** before using the MCP server
    - Review generated operations when possible
    - Use with caution in production environments
    - Be aware that complex operations could potentially affect your Maya scene

!!! info "Maya Command Port"

    The server uses Maya's Command Port for communication:

    - This is a standard Maya feature for external communication
    - No additional security measures are implemented
    - Ensure your Maya installation is secure and up-to-date
    - Consider the implications of allowing external access to Maya

## Resources

- [Maya MCP Server Repository](https://github.com/PatrickPalmer/MayaMCP) - Official repository and documentation
- [Autodesk Maya](https://www.autodesk.com/products/maya) - Official Maya documentation
- [Maya Python API](https://help.autodesk.com/view/MAYAUL/2023/ENU/?guid=Maya_SDK_Python_ref_index_html) - Reference for Maya scripting
- [Model Context Protocol](https://modelcontextprotocol.io/) - MCP specification and documentation

## Developer Notes

The Maya MCP server is designed to be easily extensible. New tools can be added by creating Python files in the `mayatools/thirdparty` directory. The server automatically discovers and registers new tools at runtime.

Key design principles:

- Tools run directly in Maya's Python environment
- No MCP decorators needed in tool files
- Functions are scoped to prevent namespace pollution
- Results are returned through Maya's command port
- Error handling is built into the communication layer
