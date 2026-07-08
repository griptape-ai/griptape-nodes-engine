# Developing Nodes

This section provides comprehensive documentation for developers building custom nodes for Griptape Nodes.

!!! tip "For AI Assistants & Coding Agents"

    All documentation in this section is available as post-processed markdown for AI coding assistants. The site exposes a full machine-readable surface; see [For Agents](../../for_agents.md) for the index.

    - **Getting Started**: [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/getting_started/index.md)
    - **Comprehensive Guide**: [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/comprehensive_guide/index.md)
    - **Example Code**: [View Python Example](https://raw.githubusercontent.com/griptape-ai/griptape-nodes/main/docs/development/custom_nodes/example_control_node.py)

    **Usage:** Point your AI assistant to these URLs with instructions like:
    `"Read this node development guide: [URL] and help me build a custom node"`

## Getting Started

If you're new to developing nodes, start with the [Getting Started Guide](getting_started.md). This guide provides a beginner-friendly introduction to the node development ecosystem and walks you through building your first node.

## Comprehensive Reference

For detailed technical information, see the [Comprehensive Node Development Guide](comprehensive_guide.md). This exhaustive reference covers:

- Node base classes (`DataNode`, `ControlNode`, `StartNode`, `EndNode`, etc.)
- Parameters, traits, containers, and lifecycle callbacks
- Async patterns (`AsyncResult`)
- Advanced UI/UX and error-handling guidance
- Creating and distributing node libraries
- Custom widget components
- Production best practices

## Practical Examples

- [Example Control Node](example_control_node.py) - A complete working example demonstrating best practices for building control nodes

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

## Documentation Structure

1. **[Getting Started](getting_started.md)** - Your first node and essential concepts
1. **[Comprehensive Guide](comprehensive_guide.md)** - Complete technical reference
1. **[Example Code](example_control_node.py)** - Practical implementation patterns
