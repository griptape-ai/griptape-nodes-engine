# Getting Started with Node Development

!!! tip "For AI Assistants & Coding Agents"

    This guide is available as post-processed markdown for AI coding assistants. The site exposes a full machine-readable surface; see [For Agents](../../for_agents.md) for the index.

    - **Getting Started** (this page): [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/getting_started/index.md)
    - **Overview**: [Markdown](https://docs.griptapenodes.com/en/stable/development/custom_nodes/index.md)
    - **Example Code**: [View Python Example](https://raw.githubusercontent.com/griptape-ai/griptape-nodes/main/docs/development/custom_nodes/example_control_node.py)

    **Usage:** Point your AI assistant to these URLs with instructions like:
    `"Read this node development guide: [URL] and help me build a custom node"`

This page is for developers who are **new to the Griptape Nodes ecosystem** and want to build custom nodes with confidence.

It’s a beginner-friendly “front door” to the deeper, exhaustive technical material in the
rest of this section — see the [Overview](index.md) for the full map of reference pages.

## What you’ll build (mentally) before you write code

At a high level:

- A **Node** is a Python class that defines **parameters** (inputs/outputs/properties) and a `process()` method.
- A **Workflow (Flow)** is a graph of nodes connected by parameters.
- Parameters are both:
    - **UI elements** (what a user sees/edits/connects), and
    - **type-checked connection points** (what can connect to what).

### Choose the right base node type

- **`DataNode`**: use when your node processes data and doesn’t need to branch execution.
- **`ControlNode`**: use when your node needs explicit execution flow (control in/out).
- **`SuccessFailureNode`**: use when you want separate control-flow outputs for success vs failure.
- **Iterative loop nodes**: the engine’s loop primitives are built on `BaseIterativeStartNode` / `BaseIterativeEndNode`.

If you’re unsure, start with a `DataNode` and only graduate to `ControlNode`/loop nodes when you need it.

## Quick start (recommended path)

If you’ve never built a Griptape Node before, this is the fastest path to a working node:

- Start from the [library template repository](https://github.com/griptape-ai/griptape-nodes-library-template/) (see the [overview](index.md#start-from-the-template-repository))
- Build a single `DataNode` first (no control flow).
- Prefer the built-in `Parameter*` helper constructs for common types.
- Validate inputs with `validate_before_node_run()`.
- Add secrets through `GriptapeNodes.SecretsManager()` when needed.

## Your first node (minimal example)

This is the smallest useful node you can build: read a string, transform it, emit a string.

```python
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class UppercaseText(DataNode):
    def __init__(self, **kwargs) -> None:
        # Always call the parent constructor so the engine can initialize
        # the node’s internal state and register the node context.
        super().__init__(**kwargs)

        # add_parameter(...) registers a Parameter with the node.
        # Parameters define:
        # - what users can configure (PROPERTY mode),
        # - what can be connected from other nodes (INPUT mode),
        # - what can be connected to other nodes (OUTPUT mode).
        self.add_parameter(
            Parameter(
                name="text",
                # A parameter's "type" is its primary data type in the engine.
                # It influences UI defaults and connection type checking.
                type="str",
                # input_types controls what types can connect INTO this parameter.
                # You can allow multiple incoming types if you need flexible wiring.
                input_types=["str"],
                # default_value is used when nothing is connected and the user
                # hasn't set a value in the UI.
                default_value="Hello Griptape Nodes",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="Input text",
            )
        )

        self.add_parameter(
            Parameter(
                name="uppercased",
                type="str",
                output_type="str",
                allowed_modes={ParameterMode.OUTPUT},
                tooltip="Uppercased output",
            )
        )

    def process(self) -> None:
        # process() is called when the node executes in a flow.
        # Read inputs via get_parameter_value(...) and write outputs via parameter_output_values.
        text = self.get_parameter_value("text") or ""
        self.parameter_output_values["uppercased"] = text.upper()
```

### Test as you go

- After adding/editing a node, run it in a simple flow and verify:
    - the parameter UI looks correct (inputs, properties, outputs)
    - output values update in the UI
    - validation errors are actionable

## Parameters: the practical model

Every parameter can be used in three “modes”:

- **Input**: accepts a connection from another node
- **Output**: provides a connection to another node
- **Property**: user-configurable value in the node UI

### Use `Parameter*` helpers for common cases

The core engine ships parameter helper constructs under `griptape_nodes.exe_types.param_types.*` such as:

- `ParameterString`, `ParameterInt`, `ParameterFloat`, `ParameterBool`
- `ParameterJson`, `ParameterDict`, `ParameterRange`
- `ParameterImage`, `ParameterAudio`, `ParameterVideo`, `Parameter3D`
- `ParameterButton`

These helpers are useful because they:

- hard-set the intended `type` / `output_type` and common `ui_options`
- often support `accept_any=True` to convert values safely
- expose several UI options as Python properties for runtime updates

If you need a quick reference, see the
[Parameter helper constructs](parameters.md#parameter-helper-constructs-parameterstring-parameterint) section of the Parameters reference.

### Containers: `ParameterList` and `ParameterDictionary`

- **`ParameterList`**: use when you want “many of the same thing” in a node UI.
    - Retrieval: `get_parameter_list_value()` flattens nested iterables.
    - Note: the current implementation drops falsey items (e.g. `0`, `False`). Preserve those by using `get_parameter_value()` and flattening manually.
- **`ParameterDictionary`**: use when you want ordered key/value entries in the UI.

### Traits: UI behaviors and validation

Traits are attached to parameters to add UI and behavior.

Common traits you’ll use:

- `Options(...)`: dropdowns (choices are stored in `ui_options` for serialization stability)
- `Slider(min_val, max_val)`: slider UI + range validation
- `FileSystemPicker(...)`: file/directory picking UI (with filters and workspace constraints)

Example: a numeric slider parameter:

```python
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.traits.slider import Slider

self.add_parameter(
    Parameter(
        name="temperature",
        type="float",
        default_value=0.7,
        tooltip="Sampling temperature (higher = more random)",
        # This can be a pure PROPERTY, or INPUT+PROPERTY if you want to allow wiring.
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        # Common pattern in the codebase: attach traits inline via the `traits` argument.
        traits={Slider(min_val=0.0, max_val=2.0)},
    )
)
```

## Validation, error handling, and user experience

For newcomers, a good default is:

- Use `validate_before_node_run()` for parameter validation
- Fail early with actionable messages (tell the user what to connect or set)
- If the node can fail but you want the workflow to continue, use `SuccessFailureNode` and route failure explicitly

Examples you can reference in the docs:

- [Start Flow](../../nodes/execution/start_flow.md) and [End Flow](../../nodes/execution/end_flow.md) for control-flow concepts and status reporting.

## Common gotchas

- **`get_parameter_list_value()` drops falsey items**: if your list can contain `0` or `False`, use `get_parameter_value()` and flatten manually.
- **`ui_options` conflicts**: if you pass both `hide=...` and `ui_options={"hide": ...}`, the `ui_options` value wins.
- **Secrets**: do not hardcode API keys. Use `GriptapeNodes.SecretsManager().get_secret(...)`.

## Secrets and configuration

When a node needs an API key or other secret:

- Register secrets in the library configuration (`griptape_nodes_library.json`)
- Read secrets via `GriptapeNodes.SecretsManager().get_secret("NAME")`

## Where to look for real examples

- **Standard library nodes**: `libraries/griptape_nodes_library/griptape_nodes_library/`
- **Engine internals** (advanced): `src/griptape_nodes/`

## Next steps

- Read the deeper technical references: [Parameters](parameters.md), [Execution and Lifecycle](execution_and_lifecycle.md), and the rest of the pages in this section
- Browse a few nodes in the standard library and copy patterns that match your use case
