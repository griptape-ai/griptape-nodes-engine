# Developing Nodes

This section provides comprehensive documentation for developers building custom nodes for Griptape Nodes.

!!! tip "For AI Assistants & Coding Agents"

    All documentation in this section is available as post-processed markdown for AI coding assistants. The site exposes a full machine-readable surface; see [For Agents](../../for_agents.md) for the index.

    - **Getting Started**: [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/getting_started/index.md)
    - **Overview** (this page): [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/index.md)
    - **Example Code**: [View Python Example](https://raw.githubusercontent.com/griptape-ai/griptape-nodes/main/docs/development/custom_nodes/example_control_node.py)

    **Usage:** Point your AI assistant to these URLs with instructions like:
    `"Read this node development guide: [URL] and help me build a custom node"`

## Introduction

Griptape Nodes are modular workflow components that enable users to build complex AI workflows through visual programming. This section covers both fundamental concepts and advanced patterns for creating robust, user-friendly nodes.

If you're new to developing nodes, start with the [Getting Started Guide](getting_started.md). It provides a beginner-friendly introduction to the node development ecosystem and walks you through building your first node.

Nodes inherit from BaseNode subclasses:

- **DataNode**: For data processing tasks
- **ControlNode**: For flow control with exec_in/out
- **StartNode**: For workflow initialization
- **EndNode**: For workflow termination

## Core Concepts

### Base Classes

- **DataNode**: Processes data without execution flow control. Use for nodes that transform or pass through data synchronously — they process immediately when their inputs are satisfied.
- **ControlNode**: Manages execution flow with exec_in/exec_out connections. Use for nodes that make external API calls or perform long-running operations — override `async def aprocess()` for async work, or yield blocking work to a background thread with `AsyncResult`. If your node calls an API and polls for results, it must be a ControlNode.
- **StartNode**: Entry points for workflows
- **EndNode**: Terminal points for workflows

### Parameters

Define inputs, outputs, and properties via the Parameter class. Parameters support:

- Type validation
- UI customization
- Connection constraints
- Default values
- Traits (Options, Slider, Button, ColorPicker)

See [Parameters](parameters.md) for the full reference.

### Process Method

The `process()` method contains core logic. Set outputs in `self.parameter_output_values`. For asynchronous work, override `async def aprocess()` instead — see [Execution and Lifecycle](execution_and_lifecycle.md).

### Node States

- **UNRESOLVED**: Initial state
- **RESOLVING**: Currently processing
- **RESOLVED**: Processing complete

### Connections

Managed via lifecycle callbacks for validation and handling. See [Execution and Lifecycle](execution_and_lifecycle.md).

### Events

Use `on_griptape_event` for reacting to workflow events.

## Setting Up

1. Install griptape-nodes
1. Use virtual environments for isolation
1. Structure projects with simple folder hierarchies
1. Import from `griptape_nodes.exe_types.*` and `griptape_nodes_library.utils.*`

## Creating a Node

### Basic Node Structure

```python
from typing import Any
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

class MyNode(DataNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = "Category"
        self.description = "Description"

        self.add_parameter(Parameter(
            name="input",
            input_types=["str"],
            type="str",
            tooltip="Input parameter"
        ))

        self.add_parameter(Parameter(
            name="output",
            output_type="str",
            tooltip="Output parameter"
        ))

    def process(self) -> None:
        val = self.get_parameter_value("input").upper()
        self.parameter_output_values["output"] = val
```

## Documentation Structure

- **[Getting Started](getting_started.md)** — Beginner-friendly walkthrough of your first node
- **[Parameters](parameters.md)** — Parameter attributes, traits, helper classes, containers, and advanced parameter patterns
- **[Parameter UI Reference](parameter_ui_reference.md)** — Parameter type to widget mapping, supported `ui_options` keys, and traits
- **[Execution and Lifecycle](execution_and_lifecycle.md)** — Lifecycle callbacks and asynchronous API integration patterns
- **[Working with the Project System](project_system.md)** — Saving files through situations, macros, and `ProjectFileParameter`
- **[Best Practices and Error Handling](error_handling.md)** — Secrets, imports, validation, error handling, and logging
- **[Authoring Libraries](authoring_libraries.md)** — Library manifests, declarations, dependency management, and contributing to the standard library
- **[Custom Widgets](custom_widgets.md)** — Custom JavaScript widget components and the widget testbed
- **[Patterns and Examples](examples.md)** — Advanced patterns from production nodes and quick-reference material
- **[Node Isolation with Workers](node_isolation_with_workers.md)** — Running a library isolated in a worker subprocess
- **[Strict Mode Reference](strict_mode.md)** — Strict-mode rules that identify isolation incompatibilities
- **[Example Control Node](example_control_node.py)** — A complete working example demonstrating best practices for building control nodes

## Start from the Template Repository

The fastest way to a production-ready node library is the official template repository:

[Griptape Nodes Library Template](https://github.com/griptape-ai/griptape-nodes-library-template/) ([readme](https://github.com/griptape-ai/griptape-nodes-library-template/blob/main/README.md))

It provides the necessary boilerplate, testing framework, and documentation patterns. The workflow:

1. **Use the template repository** - Create your own repository from the GitHub template
1. **Set up your environment** - Clone the repo to your Griptape Nodes workspace directory
1. **Configure your library** - Rename directories and update package information in `pyproject.toml`
1. **Create your nodes** - Define node classes (either ControlNode or DataNode) with appropriate parameters
1. **Implement your logic** - Code the required `process()` method and any additional functionality
1. **Configure library metadata** - Set up your library.json file with nodes and category information
1. **Register with the engine** - Add your library to Griptape Nodes through the settings interface
1. **Test and use** - Create flows using your custom nodes in the Griptape Nodes interface

To understand how to design your nodes, explore the patterns used in the standard library: [node reference](../../nodes/overview.md).

## Quick Links

- **Custom Scripts**: [Retained Mode Scripting](../retained_mode.md) - Automate and script the engine with Python
