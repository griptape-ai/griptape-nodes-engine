# Comprehensive Node Development Guide

!!! tip "For AI Assistants & Coding Agents"

    This guide is available as post-processed markdown for AI coding assistants. The site exposes a full machine-readable surface; see [For Agents](../for_agents.md) for the index.

    - **Comprehensive Guide** (this page): [Markdown](https://docs.griptapenodes.com/en/stable/developing_nodes/comprehensive_guide/index.md)
    - **Getting Started**: [Markdown](https://docs.griptapenodes.com/en/stable/developing_nodes/getting_started/index.md)
    - **Example Code**: [View Python Example](https://raw.githubusercontent.com/griptape-ai/griptape-nodes/main/docs/developing_nodes/example_control_node.py)

    **Usage:** Point your AI assistant to these URLs with instructions like:
    `"Read this node development guide: [URL] and help me build a custom node"`

## Table of Contents

1. [Introduction](#introduction)
1. [Core Concepts](#core-concepts)
1. [Setting Up](#setting-up)
1. [Creating a Node](#creating-a-node)
1. [Parameters](#parameters)
1. [Advanced Parameter Patterns](#advanced-parameter-patterns)
1. [Lifecycle Callbacks](#lifecycle-callbacks)
1. [Best Practices](#best-practices)
1. [Working with the Project System](#working-with-the-project-system)
1. [Advanced Topics](#advanced-topics)
1. [Modern UI/UX Patterns](#modern-uiux-patterns)
1. [Production Error Handling](#production-error-handling)
1. [Logging Best Practices](#logging-best-practices)
1. [Flexible Artifact Processing](#flexible-artifact-processing)
1. [Creating Node Libraries](#creating-node-libraries)
1. [Custom Widget Components](#custom-widget-components)
1. [Library Structure with uv Dependency Management](#library-structure-with-uv-dependency-management)
1. [Two-Mode UI Pattern (Simple + Custom)](#two-mode-ui-pattern-simple-custom)
1. [Music/Audio Generation API Patterns](#musicaudio-generation-api-patterns)
1. [Documentation Patterns for Node Libraries](#documentation-patterns-for-node-libraries)
1. [Contributing to the Standard Library](#contributing-to-the-standard-library)
1. [Appendix](#appendix)

## Introduction

Griptape Nodes are modular workflow components that enable users to build complex AI workflows through visual programming. This guide covers both fundamental concepts and advanced patterns for creating robust, user-friendly nodes.

Nodes inherit from BaseNode subclasses:

- **DataNode**: For data processing tasks
- **ControlNode**: For flow control with exec_in/out
- **StartNode**: For workflow initialization
- **EndNode**: For workflow termination

## Core Concepts

### Base Classes

- **DataNode**: Processes data without execution flow control. Use for nodes that transform or pass through data synchronously — they process immediately when their inputs are satisfied.
- **ControlNode**: Manages execution flow with exec_in/exec_out connections. Use for nodes that make external API calls, perform long-running operations, or need `AsyncResult` to yield work to a background thread. If your node calls an API and polls for results, it must be a ControlNode.
- **StartNode**: Entry points for workflows
- **EndNode**: Terminal points for workflows

### Parameters

Define inputs, outputs, and properties via the Parameter class. Parameters support:

- Type validation
- UI customization
- Connection constraints
- Default values
- Traits (Options, Slider, Button, ColorPicker)

### Process Method

The `process()` method contains core logic. Set outputs in `self.parameter_output_values`.

### Node States

- **UNRESOLVED**: Initial state
- **RESOLVING**: Currently processing
- **RESOLVED**: Processing complete

### Connections

Managed via lifecycle callbacks for validation and handling.

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

## Parameters

### Parameter Attributes

All Parameter attributes:

- **name**: str, unique identifier, no whitespace
- **tooltip**: str or list[dict] for UI help text
- **default_value**: Any default value
- **type**: str (e.g., "str", "list[str]", ParameterTypeBuiltin.STR.value)
- **input_types**: list[str] for incoming connection types
- **output_type**: str for outgoing connection type
- **allowed_modes**: set[ParameterMode] {INPUT, OUTPUT, PROPERTY}
- **ui_options**: dict for UI customization
- **converters**: list\[Callable\[[Any], Any\]\] for value transformation
- **validators**: list\[Callable\[[Parameter, Any], None\]\] for validation
- **hide/hide_label/hide_property**: common UI flags (also available via `ui_options`; `ui_options` wins on conflict)
- **allow_input/allow_property/allow_output**: convenience flags for configuring modes (ignored if `allowed_modes` is explicitly set)
- **settable**: bool (default True) - False for computed/output parameters
- **serializable**: bool (default True) - set False for non-serializable values (drivers, file handles, etc.)
- **user_defined**: bool (default False)
- **private**: bool (default False) - hide from general user editing (library/internal use)
- **parent_container_name**: str|None — assigns this parameter as a child of a `ParameterContainer` (i.e. a `ParameterList` or `ParameterDictionary`). Used for list-like ownership.
- **parent_element_name**: str|None — nests this parameter under a `ParameterGroup` (a UI grouping element). Used for visual grouping in the node UI.

### Traits

Add functionality via `add_trait()`:

- **Options**: `Options(choices=list[str] | list[tuple[str, Any]], show_search: bool = True, search_filter: str = "")`
- **Slider**: `Slider(min_val: float, max_val: float)`
- **Button**: `Button(label: str = "", variant=..., size=..., button_link=... | on_click=..., get_button_state=...)`
- **ColorPicker**: `ColorPicker(format="hex")`
- **FileSystemPicker**: `FileSystemPicker(...)` (file/directory selection UI)

### Parameter helper constructs (`ParameterString`, `ParameterInt`, ...)

Griptape Nodes includes a set of convenience Parameter subclasses under `griptape_nodes.exe_types.param_types.*`.
They exist to make common parameter patterns **simple, consistent, and runtime-mutable** (many expose UI options as Python properties).

#### Quick reference table

| Helper            | Enforced `type` / `output_type`               | Default `input_types` behavior                          | Key UI convenience args                                                            | Notes                                                                 |
| ----------------- | --------------------------------------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `ParameterString` | `"str"` / `"str"`                             | `accept_any=True` → `["any"]` with converter to `str`   | `markdown`, `multiline`, `placeholder_text`, `is_full_width`                       | `type`, `output_type`, and `input_types` constructor args are ignored |
| `ParameterBool`   | `"bool"` / `"bool"`                           | `accept_any=True` → `["any"]` with converter to `bool`  | `on_label`, `off_label`                                                            | Converts common strings like `"true"/"false"`, `"yes"/"no"`           |
| `ParameterInt`    | `"int"` / `"int"`                             | `accept_any=True` → `["any"]` with converter to `int`   | `step`, `slider`, `min_val`, `max_val`, `validate_min_max`                         | Adds constraint traits (Clamp/MinMax/Slider) based on args            |
| `ParameterFloat`  | `"float"` / `"float"`                         | `accept_any=True` → `["any"]` with converter to `float` | `step`, `slider`, `min_val`, `max_val`, `validate_min_max`                         | Adds constraint traits (Clamp/MinMax/Slider) based on args            |
| `ParameterDict`   | `"dict"` / `"dict"`                           | `accept_any=True` → `["any"]` with converter to `dict`  | (none)                                                                             | Uses `griptape_nodes.utils.dict_utils.to_dict()` for conversion       |
| `ParameterJson`   | `"json"` / `"json"`                           | `accept_any=True` → `["any"]` with converter to JSON    | `button`, `button_label`, `button_icon`                                            | Uses `json_repair.repair_json()` for robust string → JSON             |
| `ParameterRange`  | `"list"` / `"list"`                           | `accept_any=True` → `["any"]` with converter to `list`  | `range_slider` + `min_val/max_val/step`, labels                                    | Range slider is only meaningful when the value is a 2-number list     |
| `ParameterImage`  | `"ImageUrlArtifact"` / `"ImageUrlArtifact"`   | `accept_any=True` → `["any"]` (no conversion)           | `clickable_file_browser`, `webcam_capture_image`, `edit_mask`, `pulse_on_run`      | Mostly UI convenience; add converters if you need type coercion       |
| `ParameterAudio`  | `"AudioUrlArtifact"` / `"AudioUrlArtifact"`   | `accept_any=True` → `["any"]` (no conversion)           | `clickable_file_browser`, `microphone_capture_audio`, `edit_audio`, `pulse_on_run` | Mostly UI convenience; add converters if you need type coercion       |
| `ParameterVideo`  | `"VideoUrlArtifact"` / `"VideoUrlArtifact"`   | `accept_any=True` → `["any"]` (no conversion)           | `clickable_file_browser`, `webcam_capture_video`, `edit_video`, `pulse_on_run`     | Mostly UI convenience; add converters if you need type coercion       |
| `Parameter3D`     | `"ThreeDUrlArtifact"` / `"ThreeDUrlArtifact"` | `accept_any=True` → `["any"]` (no conversion)           | `clickable_file_browser`, `expander`, `pulse_on_run`                               | Mostly UI convenience; add converters if you need type coercion       |
| `ParameterButton` | `"button"` / `"str"`                          | `["str", "any"]`                                        | `label`, `variant`, `size`, `icon`, `state`, `href` / `on_click`                   | **Label is display text; `default_value` is stored value**            |

#### Shared behavior across helpers

- All helpers forward the standard `Parameter` constructor knobs (`allowed_modes` or `allow_input/allow_property/allow_output`, `hide/hide_label/hide_property`, `settable`, `serializable`, etc.).
- Many helpers default `accept_any=True`. When enabled, the helper typically sets `input_types=["any"]` and prepends a converter (e.g. `ParameterString` converts any input to `str`). Turn this off if you want strict typing.
- If you provide both an explicit convenience parameter (e.g. `hide=True`) and the same key in `ui_options` (e.g. `ui_options={"hide": False}`), **`ui_options` wins** and Griptape Nodes will warn about the conflict.

#### Detailed helper notes

##### `ParameterString`

- Enforces `type="str"` and `output_type="str"`.
- `accept_any=True` converts `None` → `""` and otherwise uses `str(value)`.
- UI convenience: `markdown`, `multiline`, `placeholder_text`, `is_full_width` (all are runtime-settable properties).

##### `ParameterBool`

- Enforces `type="bool"` and `output_type="bool"`.
- `accept_any=True` converts common string representations (e.g. `"true"`, `"yes"`, `"on"`, `"1"`) to `True` and (`"false"`, `"no"`, `"off"`, `"0"`) to `False`.
- UI convenience: `on_label`, `off_label` (runtime-settable properties).

##### `ParameterInt` / `ParameterFloat` (via `ParameterNumber`)

- Enforces numeric `type` / `output_type` and can prepend a converter when `accept_any=True`.
- `step`: stored in `ui_options["step"]` and validated (value must be a multiple of the current step).
- `slider`, `min_val`, `max_val`, `validate_min_max`: adds one of these constraint traits based on priority:
    - `Slider(min_val, max_val)` if `slider=True`
    - `MinMax(min_val, max_val)` if `validate_min_max=True`
    - `Clamp(min_val, max_val)` if `min_val` and `max_val` are provided

##### `ParameterJson`

- Enforces `type="json"` and `output_type="json"`.
- `accept_any=True` attempts to repair/parse JSON strings using `json_repair.repair_json()` (and will also attempt to stringify non-string inputs).
- UI convenience: optional editor button (`button`, `button_label`, `button_icon`).

##### `ParameterDict`

- Enforces `type="dict"` and `output_type="dict"`.
- `accept_any=True` uses `to_dict(...)` to coerce common inputs into a dict.

##### `ParameterRange`

- Enforces `type="list"` and `output_type="list"`.
- `accept_any=True` coerces `None` → `[]`, list → list, and any other value → `[value]`.
- UI convenience: `range_slider` (a nested `ui_options["range_slider"]` object) with `min_val/max_val/step` and label visibility options.
- The range slider UI is only applicable when the value is a list of exactly two numeric values.

##### `ParameterImage` (Recommended for Image Parameters)

**Always use `ParameterImage` instead of generic `Parameter` for image inputs/outputs.** It provides:

- Automatic `type="ImageUrlArtifact"` and `output_type="ImageUrlArtifact"`
- Built-in UI options for file browser, webcam capture, and mask editing
- Consistent behavior across all image-handling nodes

**Basic Usage:**

```python
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage

# Input image parameter
self.add_parameter(
    ParameterImage(
        name="input_image",
        tooltip="Input image for processing",
        allow_output=False,  # Input only
    )
)

# Output image parameter
self.add_parameter(
    ParameterImage(
        name="output_image",
        tooltip="Generated image result",
        allow_input=False,   # Output only
        allow_property=False,
    )
)
```

**Available UI Options:**

- `clickable_file_browser`: Enable file browser for image selection
- `webcam_capture_image`: Enable webcam capture
- `edit_mask`: Enable mask editing overlay
- `pulse_on_run`: Visual feedback when image updates

**Dynamic Visibility Pattern:**

For parameters that should only appear for certain model types:

```python
def __init__(self, **kwargs) -> None:
    super().__init__(**kwargs)

    # Add image parameter (hidden by default)
    self.add_parameter(
        ParameterImage(
            name="input_image",
            tooltip="Input image for image-to-image generation",
            allow_output=False,
        )
    )

    # Initialize visibility based on default model
    self._initialize_parameter_visibility()

def _initialize_parameter_visibility(self) -> None:
    """Initialize parameter visibility based on default model."""
    model = self.get_parameter_value("model") or "default"
    if model in ["model-with-image-support", "another-model"]:
        self.show_parameter_by_name("input_image")
    else:
        self.hide_parameter_by_name("input_image")

def after_value_set(self, parameter: Parameter, value: Any) -> None:
    """Update visibility when model changes."""
    if parameter.name == "model":
        if value in ["model-with-image-support", "another-model"]:
            self.show_parameter_by_name("input_image")
        else:
            self.hide_parameter_by_name("input_image")
            self.set_parameter_value("input_image", None)  # Clear when hiding

    return super().after_value_set(parameter, value)
```

**Why Use `ParameterImage` Over Generic `Parameter`:**

| Aspect      | Generic `Parameter`               | `ParameterImage`                            |
| ----------- | --------------------------------- | ------------------------------------------- |
| Type safety | Manual `type`/`input_types` setup | Automatic artifact types                    |
| UI features | Manual `ui_options` configuration | Built-in file browser, webcam, mask editing |
| Consistency | Varies by implementation          | Standardized across nodes                   |
| Maintenance | More boilerplate code             | Less code, cleaner                          |

**Legacy Pattern (Avoid):**

```python
# ❌ Don't do this - use ParameterImage instead
self.add_parameter(
    Parameter(
        name="input_image",
        input_types=["ImageArtifact", "ImageUrlArtifact", "str"],
        type="ImageArtifact",
        default_value=None,
        tooltip="Input image",
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        ui_options={"display_name": "Input Image"},
    )
)
```

**Why `ParameterImage` is better:**

- **Standardized type conversion logic:** Handles ImageArtifact, ImageUrlArtifact, and string inputs consistently
- **Built-in UI features:** File browser, webcam capture, mask editing
- **Less boilerplate:** Automatically configures types and options
- **Robust error handling:** Gracefully handles various input formats (URLs, file paths, data URIs)

**Recommended Pattern:**

```python
# ✅ Use ParameterImage for cleaner, more maintainable code
self.add_parameter(
    ParameterImage(
        name="input_image",
        tooltip="Input image",
        allow_output=False,
    )
)
```

`ParameterImage` standardizes how your node handles different image input types, reducing conversion errors and improving reliability.

##### `ParameterAudio` / `ParameterVideo` / `Parameter3D`

- Enforce their corresponding `*UrlArtifact` `type` / `output_type` (e.g., `AudioUrlArtifact`, `VideoUrlArtifact`, `ThreeDUrlArtifact`).
- These helpers primarily provide UI options (file browser / capture / editing / expanders). If you need coercion from e.g. `str` → artifact, supply `converters` and/or handle it in your node's `before_value_set()` / `process()` logic.
- Follow the same patterns as `ParameterImage` for these media types.

##### `ParameterButton`

Buttons provide interactive UI elements that trigger actions when clicked, such as updating parameters, performing calculations, or navigating between states.

**Basic Properties:**

- Enforces `type="button"` and `output_type="str"`
- By default, it's a **property-only** UI element (`allow_property=True`, `allow_input=False`, `allow_output=False`)
- Accepts either `href="..."` (simple link) or `on_click=...` (custom callback) parameters
- `label` is the display text shown on the button
- `icon` adds a visual icon (optional)
- `icon_position` controls icon placement ("left" or "right", defaults to "left")

**Important:** The `on_click` handler and `href` are passed **directly to `ParameterButton`** as parameters, not via the `Button` trait.

**Implementation Pattern:**

Buttons must be wrapped in a `ParameterButtonGroup` container:

```python
from griptape_nodes.exe_types.core_types import ParameterButtonGroup
from griptape_nodes.exe_types.param_types.parameter_button import ParameterButton
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload

class MyNode(DataNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Create button group with context manager
        with ParameterButtonGroup(name="my_button_group") as button_group:
            ParameterButton(
                name="update_button",
                label="Update Date/Time",
                icon="calendar",
                on_click=self._handle_button_click,  # Pass on_click directly
            )
        self.add_node_element(button_group)

        # Add parameter that will be updated by button
        self.add_parameter(
            Parameter(
                name="display_value",
                tooltip="Value updated by button",
                type=ParameterTypeBuiltin.STR.value,
                allowed_modes={ParameterMode.PROPERTY},
                ui_options={
                    "display_name": "Display Value",
                    "readonly": True,  # Prevent manual editing
                },
                default_value="Click button to update",
            )
        )

    def _handle_button_click(
        self,
        button: Button,
        button_payload: ButtonDetailsMessagePayload,
    ) -> None:
        """Button click handler.

        Args:
            button: The Button trait instance
            button_payload: Contains click event details
        """
        # Update parameter value
        new_value = "Updated at " + datetime.now().strftime("%H:%M:%S")
        self.set_parameter_value("display_value", new_value)
```

**Multiple Buttons in a Group:**

```python
with ParameterButtonGroup(name="navigation_buttons") as nav_buttons:
    ParameterButton(
        name="previous",
        label="Previous",
        icon="arrow-left",
        on_click=self._previous_item,  # Pass on_click directly
    )
    ParameterButton(
        name="next",
        label="Next",
        icon="arrow-right",
        icon_position="right",
        on_click=self._next_item,  # Pass on_click directly
    )
self.add_node_element(nav_buttons)
```

**Common Use Cases:**

1. **Update Display Values**

    ```python
    def _update_datetime(self, button: Button, button_payload: ButtonDetailsMessagePayload) -> None:
        """Update datetime display when button is clicked."""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.set_parameter_value("datetime_display", current_time)
    ```

1. **Navigate Through Items**

    ```python
    def _next_image(self, button: Button, button_payload: ButtonDetailsMessagePayload) -> None:
        """Increment index and update display."""
        current_index = self.get_parameter_value("image_index")
        self.set_parameter_value("image_index", current_index + 1)
        self._update_display()
    ```

1. **Trigger Calculations**

    ```python
    def _calculate(self, button: Button, button_payload: ButtonDetailsMessagePayload) -> None:
        """Perform calculation and update result parameter."""
        input_value = self.get_parameter_value("input")
        result = self._perform_complex_calculation(input_value)
        self.set_parameter_value("result", result)
    ```

1. **Reset to Defaults**

    ```python
    def _reset(self, button: Button, button_payload: ButtonDetailsMessagePayload) -> None:
        """Reset parameters to default values."""
        self.set_parameter_value("counter", 0)
        self.set_parameter_value("display", "")
    ```

**Link Buttons (Alternative to `on_click`):**

For simple navigation to external URLs:

```python
ParameterButton(
    name="docs_link",
    label="View Documentation",
    icon="external-link",
    href="https://docs.griptape.ai",  # Pass href directly
)
```

**Best Practices:**

- Use descriptive button labels that clearly indicate the action
- Choose appropriate icons that match the action (e.g., "calendar" for date/time, "arrow-left"/"arrow-right" for navigation)
- Keep button handlers simple and focused on a single action
- Use read-only parameters for values that should only be updated by buttons
- Avoid triggering expensive operations directly in button handlers (consider using flags that `process()` checks instead)
- Group related buttons together in a single `ParameterButtonGroup`

**Common Patterns:**

| Pattern        | Button Action                      | Updated Parameter Type      | Use Case                         |
| -------------- | ---------------------------------- | --------------------------- | -------------------------------- |
| Update Display | Updates a read-only text parameter | `PROPERTY` (readonly)       | Show current time, status, count |
| Navigation     | Increments/decrements an index     | Hidden `PROPERTY` parameter | Image carousel, list browsing    |
| Toggle State   | Switches between states            | `PROPERTY` parameter        | Enable/disable features          |
| Trigger Action | Sets a flag checked by `process()` | Hidden `PROPERTY` parameter | Refresh data, recalculate        |

**Complete Example:**

See `example_control_node.py` and `image_carousel.py` for working implementations that demonstrate:

- Button creation with icons
- Button group usage
- Updating read-only parameters
- Handler method signatures
- Navigation patterns
- Locale-appropriate datetime formatting

### Containers

- **ParameterList**: A container parameter that owns multiple child `Parameter` items (use `get_parameter_list_value()` to flatten values)
- **ParameterDictionary**: A container parameter that owns ordered key/value pairs (distinct from `ParameterDict`, which is a `dict`-typed value parameter helper)
- **ParameterGroup**: For UI grouping

**Container semantics (important):**

- Container parameters are represented as `ParameterContainer` objects in the engine. They are **always truthy**, even when empty (they override `__bool__()` to avoid bugs with stale cached values).
- `ParameterList` supports several UI convenience options (e.g. `collapsed`, grid display, and column count) that are merged into `ui_options` at runtime.
- `ParameterDictionary` is an ordered collection of key/value pair children (internally represented as a list to preserve order).

**`parent_container_name` vs `parent_element_name` — critical distinction:**

Parameters have two separate parent-pointer attributes that serve different purposes:

| Attribute               | Points to                                                     | Purpose                                                                                                                                                                                                     |
| ----------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parent_container_name` | `ParameterContainer` (`ParameterList`, `ParameterDictionary`) | **Ownership.** The parameter is a child of a list/dictionary container. The engine uses this for `add_parameter()`, child cleanup, value aggregation, and serialization/reload.                             |
| `parent_element_name`   | `ParameterGroup`                                              | **UI grouping.** The parameter is visually nested under a collapsible group in the node UI. The engine uses this for `add_parameter()` placement, `_remove_existing_*()` lookups, and serialization/reload. |

**Do NOT confuse them.** If you use `parent_container_name` when you should be using `parent_element_name` (or vice versa), the parameter will:

1. Appear at the node root instead of inside the intended group/container
1. Not be cleaned up between runs (e.g. stale outputs persist)
1. Fail to restore after save/reload — the reload handler looks for a `ParameterContainer` or `ParameterGroup` by the name you specified, and if the type doesn't match, the parameter is silently dropped before its saved values are applied

**Rule of thumb:**

- Putting a parameter inside a **`ParameterList`** or **`ParameterDictionary`**? → use `parent_container_name`
- Putting a parameter inside a **`ParameterGroup`** for visual organization? → use `parent_element_name`

```python
# ✅ CORRECT: Nesting under a ParameterGroup for UI grouping
param = ParameterImage(
    name="cell_0_0",
    parent_element_name=self._grid_cells_group.name,  # ParameterGroup
    ...
)

# ❌ WRONG: Using parent_container_name for a ParameterGroup
param = ParameterImage(
    name="cell_0_0",
    parent_container_name=self._grid_cells_group.name,  # BUG: this is a ParameterGroup, not a ParameterContainer
    ...
)
```

### ParameterList Pattern

For parameters accepting multiple inputs of the same type:

```python
self.add_parameter(
    ParameterList(
        name="tools",
        input_types=["Tool", "list[Tool]"],
        default_value=[],
        tooltip="Connect individual tools or a list of tools",
        allowed_modes={ParameterMode.INPUT},
    )
)

# Retrieve in process method
tools = self.get_parameter_list_value("tools")  # Always returns a list
for tool in tools:
    # Process each tool
```

**Benefits:**

- Multiple connection points in UI
- Automatic aggregation of inputs
- Flexible workflow design
- Follows Griptape design patterns

**Important behavior note:** `get_parameter_list_value()` flattens nested iterables and **drops falsey items**
(e.g. `0`, `False`, `""`, empty dicts/lists). If you need to preserve falsey values, use `get_parameter_value()` and handle flattening yourself.

### Common Parameter Patterns

#### Search Input with Placeholder

```python
Parameter(
    name="search_query",
    input_types=["str"],
    type="str",
    tooltip="Search term to find models",
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    ui_options={"placeholder_text": "e.g., llama, bert, stable-diffusion"}
)
```

#### Full-Width List Output

```python
Parameter(
    name="results",
    output_type="list[dict]",
    type="list[dict]",
    tooltip="Search results with full information",
    allowed_modes={ParameterMode.OUTPUT},
    ui_options={"is_full_width": True}
)
```

#### Multiline Text Input

```python
Parameter(
    name="prompt",
    input_types=["str"],
    type="str",
    tooltip="Description of desired output",
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    ui_options={"multiline": True, "placeholder_text": "Describe what you want..."}
)
```

#### File Upload with Browser

```python
Parameter(
    name="image",
    input_types=["ImageArtifact", "ImageUrlArtifact", "str"],
    type="ImageArtifact",
    tooltip="Input image file",
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    ui_options={"clickable_file_browser": True}
)
```

## Advanced Parameter Patterns

### Dynamic Parameter Visibility

Use `after_value_set()` callback to create context-aware UIs:

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    """Update parameter visibility based on model selection."""
    if parameter.name == "model":
        if value == "text-to-image":
            self.hide_parameter_by_name("input_image")
            self.show_parameter_by_name("prompt")
        elif value == "image-to-image":
            self.show_parameter_by_name("input_image")
            self.show_parameter_by_name("prompt")

    return super().after_value_set(parameter, value)
```

### Dynamic Options Updates

Update parameter choices at runtime:

```python
from griptape_nodes.traits.options import Options

def _update_option_choices(self, param_name: str, choices: list, default_value: str):
    """Update Options trait choices dynamically."""
    param = self.get_parameter_by_name(param_name)
    if not param:
        return

    # Traits are stored as child elements on the Parameter
    # (most commonly, you'll be updating an Options trait)
    for trait in param.find_elements_by_type(Options):
        trait.choices = choices
        break
    self.set_parameter_value(param_name, default_value)
```

### Dynamic Parameter Schemas with ParameterTransitionComponent

Some nodes expose a dropdown — a model picker, a mode selector, an operation chooser — where each choice implies a different set of input and output parameters. The naive approach (clear every parameter, rebuild from scratch) destroys every connection the user set up. `ParameterTransitionComponent` solves this by computing the diff between the current parameter surface and the one the new choice needs, then acting one of four ways per name:

- **Preserve** — the name exists on both sides with identical signatures. Nothing is touched.
- **Replace** — the name exists on both sides but the signature changed (types, modes, or both). The component captures the existing incoming and outgoing connections, removes the old parameter, adds a new one with the new signature, and then re-dispatches each captured connection through `CreateConnectionRequest`. The platform's connection handler validates type compatibility, so connections that are still valid under the new signature come back; connections that are no longer valid are dropped. Because connection re-creation goes through the same path as a fresh connection, values re-flow through incoming edges the normal way.
- **Remove** — the name no longer exists in the desired schema. Removed via `RemoveParameterFromNodeRequest`, which cleans up all connections.
- **Add** — the name is new. Added via the caller-supplied factory.

One consequence of always-replace: a replaced parameter's stored *property* value is lost (the new Parameter starts empty). Connections and their re-flowed values are what survive. For nodes that don't want this behavior for certain same-name-same-signature cases, declaring identical signatures keeps the parameter in the preserve bucket.

#### Walkthrough — Multi-model image generation (hypothetical)

> **Note on this example.** The `MultiModelImageGenerator` node used below is **hypothetical** — it does not exist in Griptape today, and this walkthrough is not a description of how any shipped node works. We use a hypothetical consolidated node here because it's the kind of node an author might build *on top of* `ParameterTransitionComponent`, and it makes the "switching between shapes" story concrete for an artist audience.

**The hypothetical node.** `MultiModelImageGenerator` has a **Model** dropdown offering **FLUX**, **SDXL**, and **Stable Diffusion 3**, plus a "— none —" option for an unconfigured starting state. Every model shares some parameters; each has its own extras. Here's the parameter surface per model:

| Parameter         | FLUX  | SDXL          | SD3               |
| ----------------- | ----- | ------------- | ----------------- |
| `prompt`          | str   | str           | str               |
| `width`           | int   | int           | int               |
| `height`          | int   | int           | int               |
| `seed`            | int   | int           | int               |
| `guidance`        | float | —             | —                 |
| `steps`           | —     | int           | —                 |
| `cfg_scale`       | —     | float         | —                 |
| `reference_image` | —     | **str (URL)** | **ImageArtifact** |
| `negative_prompt` | —     | —             | str               |

Notice `reference_image` appears on both SDXL and SD3 but with different types — SDXL's driver accepts a URL string, SD3's accepts a Griptape-native `ImageArtifact`. Real image-to-image APIs diverge exactly this way: some want a URL, some want uploaded bytes, some want a native artifact object.

**The user session.** An artist opens a workflow. The `MultiModelImageGenerator` node starts with no model selected — its parameter surface is empty apart from the Model dropdown itself.

**Step 1 — First selection (FLUX).** The artist picks **FLUX**. Every FLUX parameter (`prompt`, `width`, `height`, `seed`, `guidance`) appears. They connect a `TextPrompt` node to `prompt`, connect a `ResolutionPicker` node to `width` and `height`, type `42` into `seed`, leave `guidance` at its default. They render.

*Under the hood:* the component sees an empty "current" set and a full "desired" set for FLUX. Every FLUX parameter lands in `to_add`. Nothing in `to_remove` or `to_preserve`.

**Step 2 — Comparing models (FLUX → SDXL).** The artist wants to see how SDXL handles the same prompt. They switch the dropdown to **SDXL**.

- `prompt`, `width`, `height` → `to_preserve`. The connections to `TextPrompt` and `ResolutionPicker` are untouched.
- `seed` → `to_preserve`. Same name, both `int`. Value `42` survives.
- `guidance` → `to_remove`. SDXL doesn't have it.
- `steps`, `cfg_scale`, `reference_image` → `to_add`. The SDXL-specific parameters appear, ready for the artist to tune.

The artist tweaks `steps`, connects an `ImageURL` node to `reference_image`, and renders. Every connection they set up in Step 1 is still live. They didn't have to redo anything.

**Step 3 — Signature change (SDXL → SD3).** Curious about SD3, they switch the dropdown again.

- `prompt`, `width`, `height`, `seed` → `to_preserve`. Signatures unchanged, so nothing is touched.
- `reference_image` → `to_replace`. Both models use the name `reference_image`, but SDXL's accepts a URL string and SD3's accepts an `ImageArtifact`. The component captures the existing connection from `ImageURL`, removes the old parameter, adds the new `ImageArtifact`-typed one, and then re-dispatches the captured connection through `CreateConnectionRequest`. The connection handler sees the source provides `str` but the new parameter accepts only `ImageArtifact` and rejects it. Net result: `reference_image` exists with the new signature, no connection, ready for the artist to connect an artifact source (e.g., a `LoadImage` node).
- `steps`, `cfg_scale` → `to_remove`. SD3 doesn't use them.
- `negative_prompt` → `to_add`. SD3's distinguishing parameter appears, empty and ready for input.

Type narrowing behaves exactly as an artist would expect. If `reference_image` had previously accepted `[ImageArtifact, str]` with a connection providing `ImageArtifact`, and the new schema narrowed it to `[ImageArtifact]`, the replace path captures the connection, rebuilds the parameter, and re-dispatches the connection — the platform's connection handler sees `ImageArtifact` is still valid and the edge comes back automatically. Only connections that are no longer type-compatible get dropped.

**Step 4 — Clearing the node (SD3 → no selection).** The artist decides this node isn't working out and selects "— none —" from the dropdown. Everything the node was managing goes to `to_remove`. `prompt`, `width`, `height`, `seed`, `reference_image`, `negative_prompt` — all gone. The parameter surface returns to just the Model dropdown. The artist can start fresh or pick a different approach.

#### What the node author writes

```python
from griptape_nodes.exe_types.param_components.parameter_transition_component import (
    ParameterTransitionComponent,
    TransitionParameter,
)

# In MultiModelImageGenerator.__init__:
self._model_params = ParameterTransitionComponent(
    self,
    manages_parameter=lambda p: p.name in self._MODEL_PARAM_NAMES,
)

# In the model-dropdown change handler:
desired = self._build_transition_parameters_for_model(selected_model)  # author's domain logic
self._model_params.transition_to(desired)
```

`_build_transition_parameters_for_model` is the author's — the component doesn't care how the list is computed (from a dataclass, a config file, a registry, hand-rolled). It only cares about the resulting `list[TransitionParameter]`. The author also decides how `manages_parameter` scopes the component's view of the node's parameters (here: a known set of model-parameter names, excluding the Model dropdown itself and any other ambient parameters the node owns).

#### API reference

**Constructor:**

```python
ParameterTransitionComponent(
    node: BaseNode,
    *,
    manages_parameter: Callable[[Parameter], bool],
)
```

- `node` — the node instance that owns the parameter surface.
- `manages_parameter` — predicate identifying which of the node's parameters this component manages. Everything outside the predicate is left alone.

**`TransitionParameter` fields:**

- `name: str` — parameter name, must be unique within a single `transition_to` call.
- `allowed_modes: frozenset[ParameterMode]` — which modes the parameter supports (`INPUT`, `PROPERTY`, `OUTPUT`). Must match the existing Parameter's `allowed_modes` for preservation.
- `input_types: frozenset[str]` — the set of types the parameter accepts as input, matching what `Parameter.input_types` will return after creation. The property applies fallbacks when no input types are declared on the underlying request, so populate this to reflect the *effective* signature (e.g., for an output-only parameter built with `output_type="X"`, use `frozenset({"X"})` because `Parameter.input_types` falls back to `[output_type]`).
- `output_type: str` — the type the parameter emits, matching what `Parameter.output_type` will return after creation. For an input-only parameter built with `input_types=[X, Y, Z]`, use `X` because the property falls back to the first input type.
- `add_request_factory: Callable[[], AddParameterToNodeRequest]` — a zero-arg callable the component invokes only for parameters that actually need adding. Typically a `functools.partial` wrapping the author's builder method.

**`TransitionPlan` fields:**

- `to_preserve: frozenset[str]` — parameters left completely untouched (name + signature both match). Connections and stored values are unaffected because nothing was dispatched.
- `to_replace: frozenset[str]` — same-named parameters whose signature differed. The component captured their existing connections, removed the old Parameter, added a new one, and re-dispatched each connection through `CreateConnectionRequest` (the platform validates type compatibility and rejects ones that are no longer valid). Stored property values on replaced parameters are lost; connection values re-flow normally.
- `to_remove: frozenset[str]` — parameters removed via `RemoveParameterFromNodeRequest`.
- `to_add: frozenset[str]` — parameters added via `AddParameterToNodeRequest`.

**Signature-match check.** Two same-named parameters are considered a match when *all three* of `allowed_modes`, `input_types`, and `output_type` match exactly. The component reads these off the existing Parameter's public properties (the same accessors used elsewhere for connection type checks). Any difference — a widened input-type list, a dropped mode, a changed output type — puts the parameter in the replace bucket. This is identity equality over the effective schema, not directional type-compatibility: two parameters that happen to be assignment-compatible but were declared differently represent different schema intent.

#### When to reach for this

- A single node's parameter surface depends on a **discrete user choice** (model picker, mode selector, operation chooser).
- Users are expected to switch between choices during normal workflow authoring and should not lose their connections each time.

#### When NOT to reach for this

- You want to **hide or show** parameters based on a value — use the [Dynamic Parameter Visibility](#dynamic-parameter-visibility) pattern with `hide_parameter_by_name` / `show_parameter_by_name`.
- You only need to **update a dropdown's options** — use the [Dynamic Options Updates](#dynamic-options-updates) pattern.
- The parameter set is fixed; only values change.

#### Working with ParameterGroups

The component manages individual parameters by name. If your dynamic surface uses groups, pass `parent_element_name` in your `add_request_factory` so newly-added parameters land in the right group; group creation and cleanup are handled by the caller.

### Advanced ParameterList Usage

Include both individual and list types for maximum flexibility:

```python
self.add_parameter(
    ParameterList(
        name="images",
        input_types=[
            "ImageArtifact",
            "ImageUrlArtifact",
            "str",
            "list",
            "list[ImageArtifact]",
            "list[ImageUrlArtifact]",
        ],
        default_value=[],
        tooltip="Input images (up to 10 images total)",
        allowed_modes={ParameterMode.INPUT},
        ui_options={"expander": True, "display_name": "Input Images"},
    )
)
```

### Controlling Parameter Order in the UI

Parameters appear in the UI in the order they are added via `add_parameter()`. This matters for user experience - related parameters should be grouped logically.

**Problem**: Base classes like `BaseImageProcessor` automatically add parameters (e.g., `input_image`) in their `__init__`, which may not be the order you want.

**Solution**: Extend `SuccessFailureNode` directly instead of `BaseImageProcessor` to gain full control over parameter ordering:

```python
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage

class ColorMatch(SuccessFailureNode):
    """Transfer colors from a reference image to a target image."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # Reference image FIRST - the source of the color palette
        self.add_parameter(
            ParameterImage(
                name="reference_image",
                tooltip="Reference image - the source of the color palette to transfer",
                ui_options={"clickable_file_browser": True, "expander": True},
            )
        )

        # Target image SECOND - the image to modify
        self.add_parameter(
            ParameterImage(
                name="target_image",
                tooltip="Target image - the image to apply the color transfer to",
                ui_options={"clickable_file_browser": True, "expander": True},
            )
        )

        # Additional parameters in desired order...
```

**When to Use This Pattern**:

- Two-image nodes where the semantic order matters (reference → target)
- Nodes requiring specific parameter groupings not provided by base classes
- When base class parameter order conflicts with your UX goals

**Trade-off**: You lose helper methods from specialized base classes, but gain complete control over the node's UI structure.

### Two-Image Processing Node Pattern

For nodes that process two images together (blending, color matching, compositing), use this pattern:

```python
from typing import Any, ClassVar
from PIL import Image

from griptape.artifacts import ImageUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes_library.utils.image_utils import (
    dict_to_image_url_artifact,
    load_pil_from_url,
    save_pil_image_with_named_filename,
)
from griptape_nodes_library.utils.file_utils import generate_filename


class TwoImageProcessor(SuccessFailureNode):
    """Base pattern for nodes processing two images."""

    CATEGORY: ClassVar[str] = "image"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # First image input
        self.add_parameter(
            ParameterImage(
                name="image_a",
                tooltip="First input image",
                ui_options={"clickable_file_browser": True, "expander": True},
            )
        )

        # Second image input
        self.add_parameter(
            ParameterImage(
                name="image_b",
                tooltip="Second input image",
                ui_options={"clickable_file_browser": True, "expander": True},
            )
        )

        # Output image
        self.add_parameter(
            ParameterImage(
                name="output_image",
                tooltip="Processed result",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def _get_image_artifact(self, param_name: str) -> ImageUrlArtifact | None:
        """Convert parameter value to ImageUrlArtifact."""
        value = self.get_parameter_value(param_name)
        if value is None:
            return None
        if isinstance(value, dict):
            return dict_to_image_url_artifact(value)
        return value

    def _process_images(self) -> None:
        """Process both images and set output."""
        image_a_artifact = self._get_image_artifact("image_a")
        image_b_artifact = self._get_image_artifact("image_b")

        if not image_a_artifact or not image_b_artifact:
            return

        # Load as PIL images
        pil_a = load_pil_from_url(image_a_artifact.value)
        pil_b = load_pil_from_url(image_b_artifact.value)

        # Process images (override in subclass)
        result_pil = self._do_processing(pil_a, pil_b)

        # Save result
        filename = generate_filename(self.name, suffix="processed")
        output_artifact = save_pil_image_with_named_filename(result_pil, filename)
        self.parameter_output_values["output_image"] = output_artifact

    def _do_processing(self, image_a: Image.Image, image_b: Image.Image) -> Image.Image:
        """Override this method with actual processing logic."""
        raise NotImplementedError

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Trigger live preview when both images are available."""
        if parameter.name in ("image_a", "image_b"):
            image_a = self.get_parameter_value("image_a")
            image_b = self.get_parameter_value("image_b")
            if image_a and image_b:
                self._process_images()

        return super().after_value_set(parameter, value)

    def process(self) -> None:
        """Main processing entry point."""
        self._process_images()
```

**Key Utilities Used**:

- `dict_to_image_url_artifact()`: Converts dict representation to artifact
- `load_pil_from_url()`: Loads PIL Image from URL (including localhost)
- `save_pil_image_with_named_filename()`: Saves PIL Image and returns artifact
- `generate_filename()`: Creates consistent filenames with node name

## Lifecycle Callbacks

All callbacks are overridable:

- **allow_incoming_connection**, **allow_outgoing_connection**: Return bool for connection validation
- **after_incoming_connection**, **after_outgoing_connection**: Handle post-connection logic
- **after_incoming_connection_removed**, **after_outgoing_connection_removed**: Handle disconnection
- **before_value_set**: Return modified value before setting
- **after_value_set**: React to parameter value changes
- **validate_before_workflow_run**, **validate_before_node_run**: Return list[Exception]|None
- **on_griptape_event**: Handle workflow events
- **initialize_spotlight**: Setup spotlight functionality
- **get_next_control_output**: Return Parameter|None for control flow

### Helper Methods

- `hide_parameter_by_name()`, `show_parameter_by_name()`
- `append_value_to_parameter()`
- `publish_update_to_parameter()`
- `show_message_by_name()`, `hide_message_by_name()`, `get_message_by_name_or_element_id()`

## Best Practices

### Core Principles

- **Descriptive names and tooltips**
- **Robust error handling with validators**
- **Single responsibility per node**
- **Use `SecretsManager` for API keys and secrets**
- **Import all dependencies at module level**
- **Idempotent process methods**

### Secrets Management

Use `GriptapeNodes.SecretsManager()` to access API keys and secrets:

```python
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

class MyNode(DataNode):
    SERVICE_NAME = "MyService"
    API_KEY_NAME = "MY_SERVICE_API_KEY"

    def _validate_api_key(self) -> str:
        api_key = GriptapeNodes.SecretsManager().get_secret(self.API_KEY_NAME)
        if not api_key:
            raise ValueError(f"Missing {self.API_KEY_NAME}")
        return api_key
```

**Key Points:**

- Import `GriptapeNodes` at module level, not inside functions
- Use `SecretsManager().get_secret()` to retrieve secrets
- Define `API_KEY_NAME` as a class constant for consistency
- Always validate that the secret exists before using it

### Import Best Practices

**Always import dependencies at module level, not inside functions:**

❌ **Bad** - Conditional/lazy imports:

```python
def _get_image_data(self, image_artifact):
    try:
        from PIL import Image  # Don't do this
        from io import BytesIO
        img = Image.open(BytesIO(image_bytes))
```

✅ **Good** - Module-level imports:

```python
# At top of file
from PIL import Image
from io import BytesIO

def _get_image_data(self, image_artifact):
    img = Image.open(BytesIO(image_bytes))
```

**Why?**

- Makes dependencies clear and visible
- Avoids redundant imports throughout the file
- Follows Python best practices (PEP 8)
- Easier to catch missing dependencies early
- Better IDE support and code completion

**Exception**: Only use conditional imports for truly optional dependencies that may not be installed:

```python
def process(self) -> None:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        error_msg = "huggingface_hub library not installed"
        self.parameter_output_values["output"] = None
        raise ImportError(error_msg)
```

### Import Organization

Organize imports in standard order with blank lines between groups:

```python
# Standard library imports
import base64
import logging
from typing import Any

# Third-party imports
import requests
from PIL import Image

# Local/Griptape imports
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
```

### Type Checking for Third-Party Libraries

When importing third-party libraries, you may encounter type checking errors. Use the appropriate `type: ignore` comment based on the situation:

#### Scenario 1: Library Installed but Missing Type Stubs

For libraries that are installed but lack type annotations (like `sklearn`, `ultralytics`, `supervision`):

```python
# ✅ Library exists but has no type stubs
from sklearn.cluster import KMeans  # type: ignore[import-untyped]
from ultralytics import YOLO  # type: ignore[import-untyped]
from supervision import Detections  # type: ignore[import-untyped]
```

#### Scenario 2: Library Not Installed in CI Type Checking Environment

For libraries that are runtime dependencies but not installed in the CI type checking environment (like `color-matcher`, specialized processing libraries):

```python
# ✅ Library not installed in type checking environment
from color_matcher import ColorMatcher  # type: ignore[reportMissingImports]
from color_matcher.normalizations import norm_img_to_uint8  # type: ignore[reportMissingImports]
```

#### When to Use Which

| Error Type             | Comment                                | Use When                         |
| ---------------------- | -------------------------------------- | -------------------------------- |
| `import-untyped`       | `# type: ignore[import-untyped]`       | Library installed, no type stubs |
| `reportMissingImports` | `# type: ignore[reportMissingImports]` | Library not in CI environment    |

**General guidance:**

- `import-untyped` is preferred when both work - it's more precise
- `reportMissingImports` is necessary when the library isn't available during type checking
- Check CI logs to determine which error you're actually getting

### Function Parameter Management

Keep function argument counts low (under 6) by using dataclasses:

❌ **Bad** - Too many parameters:

```python
def process_bbox(self, x: int, y: int, width: int, height: int,
                 dilation_percent: float, img_width: int, img_height: int):
    # Process bounding box
```

✅ **Good** - Use dataclass:

```python
from dataclasses import dataclass

@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int
    dilation_percent: float
    img_width: int
    img_height: int

def process_bbox(self, bbox: BoundingBox):
    # Process bounding box using bbox.x, bbox.y, etc.
```

**Benefits:**

- Improved readability
- Type safety
- Easier to maintain
- Self-documenting code

### Code Quality

**Additional linting best practices:**

- Remove trailing whitespace from all lines (including blank lines)
- Use consistent indentation (spaces only, no tabs)
- Keep lines under 120 characters when possible
- Use descriptive variable names
- Avoid adding unnecessary Python packaging scaffolding. Create `__init__.py` files only when you actually want a package (or need them for your chosen packaging approach).

#### Pre-commit checks (required)

Before committing in `griptape-nodes`, run formatting and checks and fix any errors:

```bash
make format
make check/lint
make check/types
```

#### Node docs + navigation

When adding a new node to the core library, also add node reference documentation:

- Create a docs page at: `docs/nodes/<category>/<node>.md`
- Add it to `mkdocs.yml` under: `nav -> Nodes Reference -> <Category>`

#### Common gotchas

- Repo-wide lint/type checks can surface issues in **untracked** files too. Avoid leaving untracked folders/files in the repo (for example, copied scratch folders) when running checks or preparing a PR.
- If ruff flags function complexity (e.g., `C901`, `PLR0912`), prefer refactoring into smaller helpers over suppressing.
- **`parent_container_name` ≠ `parent_element_name`**: These two `Parameter` attributes look similar but serve completely different purposes. `parent_container_name` is for `ParameterContainer` (list/dictionary ownership), `parent_element_name` is for `ParameterGroup` (UI grouping). Mixing them up causes parameters to land at the node root, skip cleanup between runs, and silently vanish on save/reload. See the [Containers](#containers) section for the full distinction.

## Working with the Project System

The **project system** is Griptape Nodes' centralized file management framework that handles file organization, naming, and saving across all workflows. It eliminates hard-coded file paths and provides a consistent, configurable approach to file operations.

### Overview

Before the project system, nodes used `StaticFilesManager.save_static_file()` with UUID-based filenames scattered across various locations. The project system replaces this with:

- **Centralized configuration**: File organization rules defined in `griptape-nodes-project.yml`
- **Named situations**: Semantic contexts for file operations (e.g., "save_node_output", "copy_external_file")
- **Template-based paths**: Dynamic path generation using macros like `{outputs}/{node_name}_{file_name_base}.{file_extension}`
- **Consistent behavior**: All nodes automatically follow the same file layout

### Key Components

#### Workspace

The root directory containing all project work. Configured in Griptape Nodes settings, it serves as the base for relative path resolution.

```
workspace/
├── griptape-nodes-project.yml    # Optional customizations
├── my_workflow/
│   ├── inputs/
│   ├── outputs/
│   ├── temp/
│   └── .griptape-nodes-previews/
```

#### Situations

Named scenarios that define:

1. **Where** files are saved (via macro templates)
1. **How** to handle collisions (create_new, overwrite, fail)
1. **Fallback** behavior if saving fails

Common situations include:

| Situation            | Purpose                | Default Macro Pattern                                                   |
| -------------------- | ---------------------- | ----------------------------------------------------------------------- |
| `save_node_output`   | Generated node outputs | `{outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}` |
| `copy_external_file` | External file imports  | `{inputs}/{node_name?:_}{parameter_name?:_}{file_name_base}...`         |
| `download_url`       | Downloaded files       | `{inputs}/{sanitized_url}`                                              |
| `save_preview`       | Thumbnail generation   | `{previews}/{source_relative_path?:/}...`                               |

#### Macros

Template strings in situations and directories that generate concrete file paths dynamically. Examples:

- `{outputs}` - resolves to the outputs directory path
- `{node_name}` - current node's name
- `{file_name_base}` - filename without extension
- `{file_extension}` - file extension
- `{_index?:03}` - auto-incrementing counter (3-digit format)

#### Directories

Logical name-to-path mappings that can be referenced in macros:

```yaml
directories:
  outputs:
    path_macro: "outputs"
  custom_renders:
    path_macro: "my_custom_path/{workflow_name}"
```

### Using the Project System in Nodes

There are two main patterns for working with project files in nodes:

#### Pattern 1: ProjectFileParameter (Recommended for Node Outputs)

Use `ProjectFileParameter` when your node has a configurable output file parameter that users might want to customize.

```python
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

class MyVideoNode(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Add regular output parameter
        self.add_parameter(Parameter(
            name="output_video",
            output_type="VideoUrlArtifact",
            tooltip="Generated video",
            allowed_modes={ParameterMode.OUTPUT}
        ))

        # Add project file parameter for output file configuration
        self._output_video_file = ProjectFileParameter(
            node=self,
            name="output_video_file",
            default_filename="output_video.mp4",
        )
        self._output_video_file.add_parameter()

    def process(self) -> None:
        # ... generate video_bytes ...

        # Use build_file() to get a ProjectFileDestination
        dest = self._output_video_file.build_file()
        saved = dest.write_bytes(video_bytes)

        # Set the output parameter with the saved location
        self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)
```

**Key Points:**

- `ProjectFileParameter` creates a UI parameter that users can configure
- Call `build_file()` to get a `ProjectFileDestination` instance
- Use `write_bytes()` to save the file
- Access the saved file's URL/path via `saved.location`

**❌ Common Mistake: Not Capturing write_bytes() Return Value**

```python
# WRONG - Don't do this:
dest = self._output_video_file.build_file()
dest.write_bytes(video_bytes)  # ❌ Return value not captured
artifact = VideoUrlArtifact(dest.location)  # Using dest, not saved file

# This will fail with: "Failed because missing required variables: file_extension, file_name_base"
```

**Why it fails:** Macro variables like `{file_extension}` and `{file_name_base}` are resolved when `write_bytes()` saves the file and returns the saved file object, not by `build_file()`. Using `dest.location` before writing causes macro resolution errors.

```python
# CORRECT:
dest = self._output_video_file.build_file()
saved = dest.write_bytes(video_bytes)  # ✅ Capture return value
artifact = VideoUrlArtifact(saved.location)  # Use saved file's resolved location
```

The `saved` object contains the fully resolved file path with all macros filled in.

#### Pattern 2: ProjectFileDestination Directly (For Utility Functions)

Use `ProjectFileDestination.from_situation()` directly in utility functions or when you don't need user configuration.

```python
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

def frames_to_video_artifact(frames: list, fps: int = 30, video_format: str = "mp4") -> VideoUrlArtifact:
    """Convert a list of frames to a VideoUrlArtifact."""
    # ... process frames into video_bytes ...

    # Save using project file system
    dest = ProjectFileDestination.from_situation(
        filename=f"video.{video_format}",
        situation="save_node_output"
    )
    saved = dest.write_bytes(video_bytes)

    return VideoUrlArtifact(saved.location)
```

**Key Points:**

- Use `from_situation()` to create a destination with a named situation
- The `filename` parameter is the base filename (will be transformed by the situation's macro)
- The situation (e.g., "save_node_output") determines the final path and collision behavior

### Migration from StaticFilesManager

#### Old Pattern (Deprecated)

```python
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
import uuid

def old_save_video(video_bytes: bytes) -> VideoUrlArtifact:
    filename = f"{uuid.uuid4()}.mp4"
    url = GriptapeNodes.StaticFilesManager().save_static_file(video_bytes, filename)
    return VideoUrlArtifact(url)
```

#### New Pattern

```python
from griptape_nodes.files.project_file import ProjectFileDestination

def new_save_video(video_bytes: bytes) -> VideoUrlArtifact:
    dest = ProjectFileDestination.from_situation(
        filename="video.mp4",
        situation="save_node_output"
    )
    saved = dest.write_bytes(video_bytes)
    return VideoUrlArtifact(saved.location)
```

**Migration Benefits:**

- No more UUID generation required
- Consistent file organization across all nodes
- User-configurable file paths via project templates
- Better file tracking and management
- Automatic handling of name collisions

### Common Situations and When to Use Them

- **`save_node_output`**: Primary situation for files generated by nodes (images, videos, audio, etc.)
- **`copy_external_file`**: When importing/copying files from external sources
- **`download_url`**: When downloading files from URLs
- **`save_preview`**: For generating thumbnail or preview images
- **`save_static_file`**: For static assets that don't change between runs

### Advanced Configuration

Users can customize the project system by creating a `griptape-nodes-project.yml` file in their workspace:

```yaml
project_template_schema_version: "0.1.0"
name: "My Custom Project"

directories:
  outputs:
    path_macro: "final_outputs/{workflow_name}"

situations:
  save_node_output:
    macro: "{outputs}/{node_name}_{file_name_base}_{_index:04}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
```

Your nodes automatically respect these customizations without any code changes.

### Best Practices

1. **Always use the project system** for saving files - never use hard-coded paths
1. **Choose the right pattern**: Use `ProjectFileParameter` for user-configurable outputs, `ProjectFileDestination` for utility functions
1. **Use semantic situations**: Pick the situation that best describes your operation
1. **Let macros handle naming**: Don't generate UUIDs or timestamps yourself - let the situation's macro and collision policy handle it
1. **Handle temporary files properly**: Use Python's `tempfile` for intermediate processing, only save final results via the project system
1. **Clean up temporary files**: Always clean up temporary files after copying to the project system

### Example: Complete Video Processing Node

```python
import tempfile
from pathlib import Path
from typing import Any

from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, AsyncResult
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.files.file import File

class ProcessVideo(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        self.add_parameter(Parameter(
            name="input_video",
            input_types=["VideoUrlArtifact"],
            type="VideoUrlArtifact",
            tooltip="Input video to process"
        ))

        self.add_parameter(Parameter(
            name="output_video",
            output_type="VideoUrlArtifact",
            tooltip="Processed video",
            allowed_modes={ParameterMode.OUTPUT}
        ))

        # Add project file parameter for output
        self._output_video_file = ProjectFileParameter(
            node=self,
            name="output_video_file",
            default_filename="processed_video.mp4",
        )
        self._output_video_file.add_parameter()

    def process(self) -> AsyncResult:
        yield lambda: self._process()

    def _process(self) -> None:
        # Get input video
        input_artifact = self.get_parameter_value("input_video")
        input_bytes = File(input_artifact.value).read_bytes()

        # Process in temporary location
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        try:
            # Write input to temp file
            temp_path.write_bytes(input_bytes)

            # ... perform processing on temp_path ...

            # Read processed result
            output_bytes = temp_path.read_bytes()

            # Save using project system
            dest = self._output_video_file.build_file()
            saved = dest.write_bytes(output_bytes)

            # Set output
            self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)

        finally:
            # Clean up temporary file
            if temp_path.exists():
                temp_path.unlink()
```

## Advanced Topics

### REST API vs SDK Integration

**Problem**: Python SDKs often lag behind REST APIs in supporting new features. Parameters available in REST API documentation may not be exposed in SDK libraries.

**When to Use REST API Directly**:

- SDK missing documented API features (e.g., `image_config` for Gemini)
- Need immediate access to new API parameters
- SDK has bugs or limitations
- Want lighter dependencies

**REST API Implementation Pattern**:

```python
import base64
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# Authentication
credentials = service_account.Credentials.from_service_account_file(
    service_account_file,
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)

def _get_access_token(self, credentials) -> str:
    """Get access token from credentials."""
    if not credentials.valid:
        credentials.refresh(Request())
    return credentials.token

# Build JSON payload matching REST API spec
payload = {
    "contents": {
        "role": "USER",
        "parts": [
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode('utf-8')
                }
            }
        ]
    },
    "generation_config": {
        "temperature": 1.0,
        "topP": 0.95,
        "candidateCount": 1,
        "response_modalities": ["TEXT", "IMAGE"],
        "image_config": {  # Feature not in SDK!
            "aspect_ratio": "16:9"
        }
    }
}

# Make authenticated request
access_token = self._get_access_token(credentials)
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

api_endpoint = f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:generateContent"

response = requests.post(api_endpoint, headers=headers, json=payload, timeout=120)
response.raise_for_status()
response_data = response.json()

# Parse JSON response (handle both camelCase and snake_case)
candidates = response_data.get("candidates", [])
for cand in candidates:
    parts_list = cand.get("content", {}).get("parts", [])
    for part in parts_list:
        if "inlineData" in part or "inline_data" in part:
            inline_data = part.get("inlineData") or part.get("inline_data", {})
            mime = inline_data.get("mimeType") or inline_data.get("mime_type")
            data_b64 = inline_data.get("data", "")
            data_bytes = base64.b64decode(data_b64)
```

**Key Considerations**:

1. **Dependencies**: Use `google-auth` instead of full SDK (`google-cloud-aiplatform`, `google-genai`)
1. **Regional Availability**: Some models only work in specific regions (e.g., `us-central1`), not `global`
1. **Model Names**: Check for `-preview` suffix differences between preview and stable models
1. **Authentication Scopes**: Use `https://www.googleapis.com/auth/cloud-platform` for Vertex AI
1. **Response Format**: Handle both camelCase (API) and snake_case (some SDKs) field names
1. **Base64 Encoding**: REST API expects base64-encoded strings for binary data
1. **Error Handling**: Parse JSON error responses for detailed error messages

**Trade-offs**:

- ✅ Immediate access to all API features
- ✅ Lighter dependencies
- ✅ Full control over requests
- ❌ More implementation work
- ❌ Must handle auth/tokens manually
- ❌ Need to track API changes yourself

### Complex Type Management Systems

For nodes that need sophisticated type negotiation between multiple parameters:

```python
class IfElse(BaseNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # Sophisticated connection tracking for type management
        self._possibility_space: list[str] = []  # Types acceptable to output target
        self._locked_type: str | None = None     # Specific type locked by input
        self._connected_inputs: set[str] = set() # Track input connections
        self._output_connected: bool = False     # Track output connections

    def _update_parameter_types(self) -> None:
        """Update all parameter types based on current state."""
        if self._locked_type:
            # Locked to specific type - everything uses that type
            self.output_if_true.input_types = [self._locked_type]
            self.output_if_false.input_types = [self._locked_type]
            self.output.output_type = self._locked_type
        elif self._possibility_space:
            # Flexible within possibility space
            self.output_if_true.input_types = self._possibility_space.copy()
            self.output_if_false.input_types = self._possibility_space.copy()
            self.output.output_type = ParameterTypeBuiltin.ALL.value
        else:
            # Default state - accept any type
            self.output_if_true.input_types = ["any"]
            self.output_if_false.input_types = ["any"]
            self.output.output_type = ParameterTypeBuiltin.ALL.value
```

**Best Practice**: Use sophisticated type management for nodes that route data between multiple inputs and outputs.

### Agentic Nodes

Inherit from ControlNode for agent management:

```python
from griptape.structures import Agent
from griptape_nodes.exe_types.node_types import ControlNode

class MyAgentNode(ControlNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_parameter(Parameter(
            name="agent_in",
            input_types=["Agent"],
            type="Agent"
        ))
        self.add_parameter(Parameter(
            name="agent_out",
            output_type="Agent"
        ))

    def process(self) -> None:
        agent_state = self.get_parameter_value("agent_in")
        agent = Agent.from_dict(agent_state) if agent_state else Agent()
        # Process with agent
        self.parameter_output_values["agent_out"] = agent.to_dict()
```

### Abstract Base Classes for Node Families

Create abstract base classes for related nodes to share common functionality:

```python
from abc import abstractmethod
from typing import Any

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode

class BaseCustomIterativeStartNode(BaseIterativeStartNode):
    """Base class for a family of custom iterative start nodes."""

    @abstractmethod
    def _get_compatible_end_classes(self) -> set[type]:
        """Return the set of End node classes that this Start node can connect to."""

    @abstractmethod
    def _get_parameter_group_name(self) -> str:
        """Return the name for the parameter group containing iteration data."""

    @abstractmethod
    def _get_exec_out_display_name(self) -> str:
        """Return the display name for the exec_out parameter."""

    @abstractmethod
    def _get_exec_out_tooltip(self) -> str:
        """Return the tooltip for the exec_out parameter."""

    @abstractmethod
    def _get_iteration_items(self) -> list[Any]:
        """Get the list of items to iterate over."""

    @abstractmethod
    def is_loop_finished(self) -> bool:
        """Return True if the loop has completed all iterations."""
```

**Best Practice**: Use abstract base classes to share common logic across node families while enforcing implementation of specific methods.

### Caching

Use ClassVar for shared resources:

```python
from typing import ClassVar, Any

class CachedModelNode(DataNode):
    _cache: ClassVar[dict[str, Any]] = {}

    def get_model(self, model_id: str) -> Any:
        if model_id not in self._cache:
            self._cache[model_id] = load_model(model_id)
        return self._cache[model_id]
```

### Hub Integration (e.g., HuggingFace)

```python
# Gated model detection
is_gated = getattr(model, 'gated', False)
model_dict['gated'] = is_gated

# Status updates for gated models
if getattr(model_info, 'gated', False):
    self.publish_update_to_parameter(
        "status",
        "🔒 GATED MODEL - May require approval"
    )
```

### ParameterMessage for External Links and Status Updates

```python
from griptape_nodes.exe_types.core_types import ParameterMessage

# External link example
ParameterMessage(
    name="model_card_link",
    title="Model Card",
    variant="info",
    value="View model documentation",
    button_link=f"https://huggingface.co/{model_id}",
    button_text="View on HuggingFace"
)

# Dynamic status message example
class MyIterativeNode(BaseIterativeStartNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # Status message parameter for real-time updates
        self.status_message = ParameterMessage(
            name="status_message",
            variant="info",
            value="",
        )
        self.add_node_element(self.status_message)

    def _update_status_message(self, status_type: str = "normal") -> None:
        """Update status message based on current state."""
        if self._total_iterations == 0:
            status = "Completed 0 (of 0)"
        elif status_type == "break":
            status = f"Stopped at {self._current_iteration_count} (of {self._total_iterations}) - Break"
        elif self.is_loop_finished():
            status = f"Completed {self._total_iterations} (of {self._total_iterations})"
        else:
            status = f"Processing {self._current_iteration_count} (of {self._total_iterations})"

        self.status_message.value = status
```

**Best Practice**: Use ParameterMessage for static external links, dynamic status updates, and deprecation notices. For a full walkthrough of the deprecation notice pattern (hidden message + `before_value_set` auto-migration), see [Deprecated Model Migration and User Notification](#deprecated-model-migration-and-user-notification).

## Modern UI/UX Patterns

### Advanced UI Options

```python
ui_options={
    "hide_property": True,      # Hide from property panel
    "pulse_on_run": True,       # Visual feedback during execution
    "expander": True,           # Collapsible parameter groups
    "is_full_width": True,      # Full-width display
    "multiline": True,          # Multi-line text input
    "placeholder_text": "...",  # Input placeholder
    "display_name": "...",      # Custom display name
    "markdown": True,           # Markdown rendering
    "compare": True,            # Comparison mode
    "clickable_file_browser": True,  # File browser integration
    "hide": True,               # ⭐ CORRECT: Completely hide parameter
    "collapsed": True,          # Start parameter groups collapsed
    "edit_mask": True,          # Enable mask editing for images
}
```

#### Hidden Parameters Best Practice

Use `"hide": True` to hide parameters from the UI (for advanced/expert settings):

```python
# ✅ CORRECT: Hidden parameter with slider
num_images_param = Parameter(
    name="num_images",
    input_types=["int"],
    type="int",
    default_value=1,
    tooltip="Number of images to generate (1-9)",
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    ui_options={
        "display_name": "Number of Images",
        "hide": True  # ⭐ CORRECT pattern
    },
)
num_images_param.add_trait(Slider(min_val=1, max_val=9))
self.add_parameter(num_images_param)

# ⚠️ LEGACY: "hidden": True exists but is rare (3 instances vs 47)
ui_options={"hidden": True}  # Less common, prefer "hide": True
```

**Common Use Cases for Hidden Parameters:**

- Advanced/expert configuration options
- Internal control signals
- Debug parameters
- Optional advanced features
- Parameters that should only be set programmatically

### Success/Failure Node Pattern

For nodes that can succeed or fail, inherit from SuccessFailureNode:

```python
from griptape_nodes.exe_types.node_types import SuccessFailureNode

class LoadImage(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Add status parameters using the helper method
        self._create_status_parameters(
            result_details_tooltip="Details about the image loading operation result",
            result_details_placeholder="Details on the load attempt will be presented here.",
        )

    def process(self) -> None:
        # Reset execution state at start
        self._clear_execution_status()

        # Clear output values to prevent stale data on errors
        self.parameter_output_values["image"] = None

        try:
            # Processing logic here
            result = load_image()
            self.parameter_output_values["image"] = result

            # Success case
            success_details = f"Image loaded successfully from {source}"
            self._set_status_results(was_successful=True, result_details=f"SUCCESS: {success_details}")

        except Exception as e:
            error_details = f"Failed to load image: {e}"
            self._set_status_results(was_successful=False, result_details=f"FAILURE: {error_details}")
            self._handle_failure_exception(e)
```

**Best Practice**: Use SuccessFailureNode for operations that can fail and need to report status to users.

### Parameter Initialization

Initialize parameter visibility on node creation:

```python
def _initialize_parameter_visibility(self) -> None:
    """Initialize parameter visibility based on default values."""
    default_model = self.get_parameter_value("model") or "default"
    if default_model == "text-only":
        self.hide_parameter_by_name("image_input")
    else:
        self.show_parameter_by_name("image_input")
```

### Artifact Path Tethering Pattern

For nodes that work with files, use the artifact tethering pattern to keep path and artifact parameters synchronized:

```python
from griptape_nodes_library.utils.artifact_path_tethering import (
    ArtifactPathTethering,
    ArtifactTetheringConfig,
)

class LoadImage(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Configuration for artifact tethering
        self._tethering_config = ArtifactTetheringConfig(
            dict_to_artifact_func=dict_to_image_url_artifact,
            extract_url_func=self._extract_url_from_image_value,
            supported_extensions=self.SUPPORTED_EXTENSIONS,
            default_extension="png",
            url_content_type_prefix="image/",
        )

        # Create artifact parameter
        self.image_parameter = Parameter(
            name="image",
            input_types=["ImageUrlArtifact", "ImageArtifact", "str"],
            type="ImageUrlArtifact",
            output_type="ImageUrlArtifact",
            ui_options={"clickable_file_browser": True},
        )

        # Create path parameter using tethering utility
        self.path_parameter = ArtifactPathTethering.create_path_parameter(
            name="path",
            config=self._tethering_config,
            display_name="File Path or URL",
        )

        # Tethering helper keeps parameters in sync
        self._tethering = ArtifactPathTethering(
            node=self,
            artifact_parameter=self.image_parameter,
            path_parameter=self.path_parameter,
            config=self._tethering_config,
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        # Delegate tethering logic to helper
        self._tethering.on_after_value_set(parameter, value)
        return super().after_value_set(parameter, value)
```

**Best Practice**: Use artifact tethering for seamless file/URL parameter synchronization.

## Production Error Handling

### Comprehensive Validation

Use `validate_before_node_run()` for complex validation:

```python
def validate_before_node_run(self) -> list[Exception] | None:
    """Validate parameters before running the node."""
    exceptions = []

    model = self.get_parameter_value("model")
    if model == "advanced":
        images = self.get_parameter_list_value("images") or []
        if len(images) > MAX_IMAGES:
            exceptions.append(ValueError(
                f"{self.name}: Maximum {MAX_IMAGES} images allowed, got {len(images)}"
            ))

    return exceptions if exceptions else None
```

### Connection Validation Patterns

For complex nodes with multiple connection requirements:

```python
def _validate_iterative_connections(self) -> list[Exception]:
    """Validate that all required connections are properly established."""
    errors = []
    node_type = self._get_base_node_type_name()

    # Check if exec_out has outgoing connections
    if not _outgoing_connection_exists(self.name, self.exec_out.name):
        errors.append(
            Exception(
                f"{self.name}: Missing required connection from 'On Each Item'. "
                f"REQUIRED ACTION: Connect {node_type} Start to interior loop nodes. "
                "The start node must connect to other nodes to execute the loop body."
            )
        )

    # Check if loop has outgoing connection to End
    if self.end_node is None:
        errors.append(
            Exception(
                f"{self.name}: Missing required tethering connection. "
                f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Node' to {node_type} End 'Loop Start Node'. "
                "This establishes the explicit relationship between start and end nodes."
            )
        )

    return errors
```

**Best Practice**: Provide detailed, actionable error messages that tell users exactly what connections are missing and how to fix them.

### Safe Defaults Pattern

Always set safe defaults before raising exceptions:

```python
def _set_safe_defaults(self) -> None:
    """Set safe default values for all outputs."""
    self.parameter_output_values["result"] = None
    self.parameter_output_values["status"] = "error"
    self.parameter_output_values["count"] = 0

def process(self) -> None:
    try:
        # Processing logic
        result = process_data()
        self.parameter_output_values["result"] = result
    except Exception as e:
        self._set_safe_defaults()
        raise RuntimeError(f"Processing failed: {str(e)}") from e
```

### URL Construction

Use `urllib.parse.urljoin()` for safe URL building:

```python
from urllib.parse import urljoin
import os

def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # Safe URL construction
    base = os.getenv("API_BASE_URL", "https://api.example.com")
    base_slash = base if base.endswith("/") else base + "/"
    api_base = urljoin(base_slash, "api/")
    self._endpoint = urljoin(api_base, "v1/process/")
```

## Logging Best Practices

### Safe Logging Pattern

Prevent logging failures from breaking execution:

```python
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)

def _log(self, message: str) -> None:
    """Safe logging with exception suppression."""
    with suppress(Exception):
        logger.info(message)
```

### Request Sanitization

Sanitize sensitive data in logs:

```python
from copy import deepcopy
import json

PROMPT_TRUNCATE_LENGTH = 100

def _log_request(self, payload: dict[str, Any]) -> None:
    """Log request with sanitized sensitive data."""
    with suppress(Exception):
        sanitized_payload = deepcopy(payload)

        # Truncate long prompts
        prompt = sanitized_payload.get("prompt", "")
        if len(prompt) > PROMPT_TRUNCATE_LENGTH:
            sanitized_payload["prompt"] = prompt[:PROMPT_TRUNCATE_LENGTH] + "..."

        # Redact base64 image data
        if "image" in sanitized_payload:
            image_data = sanitized_payload["image"]
            if isinstance(image_data, str) and image_data.startswith("data:image/"):
                parts = image_data.split(",", 1)
                header = parts[0] if parts else "data:image/"
                b64_len = len(parts[1]) if len(parts) > 1 else 0
                sanitized_payload["image"] = f"{header},<base64 data length={b64_len}>"

        self._log(f"Request: {json.dumps(sanitized_payload, indent=2)}")
```

## Flexible Artifact Processing

### Duck Typing for Artifacts

Handle multiple artifact formats gracefully:

```python
def _extract_image_value(self, image_input: Any) -> str | None:
    """Extract string value from various image input types."""
    if isinstance(image_input, str):
        return image_input

    try:
        # ImageUrlArtifact: .value holds URL string
        if hasattr(image_input, "value"):
            value = getattr(image_input, "value", None)
            if isinstance(value, str):
                return value

        # ImageArtifact: .base64 holds raw or data-URI
        if hasattr(image_input, "base64"):
            b64 = getattr(image_input, "base64", None)
            if isinstance(b64, str) and b64:
                return b64
    except Exception as e:
        self._log(f"Failed to extract image value: {e}")

    return None
```

### Image Format Conversion for External APIs

**Problem**: External APIs often have strict format requirements (e.g., JPEG, PNG, WebP only), but cameras may save images in unsupported formats like MPO (Multi Picture Object) for 3D/burst photos.

**Solution**: Automatically detect and convert unsupported formats:

```python
# Import at top of file
from PIL import Image
from io import BytesIO

def _get_image_data(self, image_artifact: ImageArtifact | ImageUrlArtifact) -> str:
    """Convert image to API-compatible format."""
    # ... extract image_bytes ...

    try:
        img = Image.open(BytesIO(image_bytes))

        # Convert unsupported formats (MPO, TIFF, BMP, etc.) to JPEG
        if img.format not in ['JPEG', 'PNG', 'WEBP']:
            self._log(f"Converting {img.format} to JPEG for API compatibility")
            # Convert to RGB if needed (for formats like MPO)
            if img.mode not in ['RGB', 'L']:
                img = img.convert('RGB')
            # Save as JPEG to bytes
            output = BytesIO()
            img.save(output, format='JPEG', quality=95)
            image_bytes = output.getvalue()
            mime_type = "image/jpeg"
        else:
            format_to_mime = {
                'JPEG': 'image/jpeg',
                'PNG': 'image/png',
                'WEBP': 'image/webp'
            }
            mime_type = format_to_mime.get(img.format, 'image/jpeg')
    except Exception as e:
        self._log(f"Could not detect image format: {e}")
        mime_type = "image/jpeg"

    # Encode as base64 data URI
    base64_data = base64.b64encode(image_bytes).decode('utf-8')
    return f"data:{mime_type};base64,{base64_data}"
```

**Key Points**:

- Apply conversion at all image input points (ImageArtifact, ImageUrlArtifact, localhost URLs)
- Use high quality (95%) to preserve image fidelity
- Handle color mode conversion (MPO often uses non-RGB modes)
- Log conversions for debugging
- Gracefully fall back to JPEG if detection fails

### Utility Function Patterns

Create reusable utility functions for common operations:

```python
# Connection checking utilities
def _outgoing_connection_exists(source_node: str, source_param: str) -> bool:
    """Check if a source node/parameter has any outgoing connections."""
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()
    source_connections = connections.outgoing_index.get(source_node)
    if source_connections is None:
        return False

    param_connections = source_connections.get(source_param)
    return bool(param_connections) if param_connections else False

def _incoming_connection_exists(target_node: str, target_param: str) -> bool:
    """Check if a target node/parameter has any incoming connections."""
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()
    target_connections = connections.incoming_index.get(target_node)
    if target_connections is None:
        return False

    param_connections = target_connections.get(target_param)
    return bool(param_connections) if param_connections else False
```

**Best Practice**: Create utility functions for common operations like connection checking, validation, and data processing.

### Flexible Image Processing

```python
def _image_to_bytes(self, image_artifact) -> bytes:
    """Convert various image artifact types to bytes."""
    if not image_artifact:
        raise ValueError("No input image provided")

    try:
        # Handle dictionary format (serialized artifacts)
        if isinstance(image_artifact, dict):
            image_url_artifact = self._dict_to_image_url_artifact(image_artifact)
            image_bytes = image_url_artifact.to_bytes()
        # Handle artifact objects directly
        elif isinstance(image_artifact, (ImageArtifact, ImageUrlArtifact)):
            image_bytes = image_artifact.to_bytes()
        else:
            # Try generic to_bytes method
            image_bytes = image_artifact.to_bytes()

        # Verify we have valid image data
        if not image_bytes or len(image_bytes) < 100:
            raise ValueError("Image data is empty or too small")

        return image_bytes

    except Exception as e:
        raise ValueError(f"Failed to extract image data: {str(e)}")
```

## Creating Node Libraries

Bundle nodes into libraries for sharing. Create `griptape_nodes_library.json`:

```json
{
  "name": "Library Name",
  "library_schema_version": "0.10.0",
  "settings": [
    {
      "description": "API keys required by nodes in this library",
      "category": "app_events.on_app_initialization_complete",
      "contents": {
        "secrets_to_register": ["MY_SERVICE_API_KEY", "MY_OTHER_API_KEY"]
      }
    }
  ],
  "metadata": {
    "author": "Author Name",
    "description": "Library description",
    "library_version": "1.0.0",
    "engine_version": "0.55.0",
    "tags": ["AI", "Image Processing"],
    "dependencies": {
      "pip_dependencies": ["pillow", "requests"],
      "pip_install_flags": ["--upgrade"]
    },
    "declarations": [
      { "type": "lifecycle_stage", "stage": "STABLE" },
      {
        "type": "model_catalog",
        "providers": {
          "anthropic": {
            "display_name": "Anthropic",
            "terms_url": "https://www.anthropic.com/legal/commercial-terms",
            "models": {
              "claude_opus_byok": {
                "display_name": "Claude Opus 4 (BYOK)",
                "family": "Claude 4",
                "provider_model_id": "claude-opus-4",
                "key_support": "REQUIRES_CUSTOMER_KEY"
              }
            }
          }
        }
      }
    ]
  },
  "widgets": [
    {
      "name": "MyWidget",
      "path": "widgets/MyWidget.js",
      "description": "Custom UI component for the node"
    }
  ],
  "categories": [
    {
      "image": {
        "title": "Image Processing",
        "description": "Image manipulation nodes",
        "color": "border-purple-500",
        "icon": "Image"
      }
    }
  ],
  "nodes": [
    {
      "class_name": "MyImageNode",
      "file_path": "image/my_image_node.py",
      "metadata": {
        "category": "image",
        "description": "Process images with AI",
        "display_name": "AI Image Processor",
        "icon": "image",
        "group": "processing",
        "declarations": [
          { "type": "model_usage", "model_ids": ["claude_opus_byok"] }
        ]
      }
    }
  ],
  "workflows": ["workflows/example_workflow.py"],
  "is_default_library": false
}
```

### Library Structure

- **settings**: Register secrets/API keys used by library nodes
    - Use `secrets_to_register` array to declare required secrets
    - Category should be `app_events.on_app_initialization_complete`
    - Secrets are accessed via `GriptapeNodes.SecretsManager().get_secret()`
- **metadata.dependencies**: PIP packages installed on library load
- **metadata.declarations** / per-node **metadata.declarations**: typed identity properties (lifecycle stage) and a library-level model catalog plus per-node references into it. See [Library and Node Declarations](#library-and-node-declarations) below.
- **widgets**: Register custom JS widget components (see [Custom Widget Components](#custom-widget-components))
- **categories**: Group nodes in UI with colors and icons
- **nodes**: List node classes, file paths, and metadata
- **workflows**: Template workflow files

**Important:** The `secrets_to_register` array tells the system which secrets your library needs. Users will be prompted to configure these secrets through the UI or environment variables.

Use flat directory structures. The engine automatically registers and loads libraries.

### Library and Node Declarations

Declarations attach typed metadata to a library or to an individual node. Each entry in a `declarations` array is an object with a `type` discriminator that selects a declaration class. Today's vocabulary covers a lifecycle-stage property and a library-level model catalog with per-node references; future engine releases add more declaration types under the same field.

Both `metadata.declarations` (library-level) and per-node `metadata.declarations` accept a list. Order in the list does not matter. The field defaults to `[]`, so libraries on older schema versions (`0.6.0`, `0.4.0`, `0.1.0`) load unchanged.

#### `lifecycle_stage`

Lifecycle stage for a library or for a specific node. Values:

| Value        | Meaning                                                             |
| ------------ | ------------------------------------------------------------------- |
| `STABLE`     | Mature; intended for production use.                                |
| `BETA`       | Feature-complete but still hardening.                               |
| `ALPHA`      | Early implementation; expect breaking changes.                      |
| `LABS`       | Exploratory; may be removed.                                        |
| `DEPRECATED` | Slated for removal; existing usage should migrate to a replacement. |

Semantics:

- **Library-level absence is intentionally distinct from `STABLE`.** A library with no `lifecycle_stage` declaration is "unstated" — consumers should surface that explicitly (`<No lifecycle stage provided by library author>`) rather than silently assume `STABLE`.
- **Node-level absence means "inherit the library's stage."** A node-level `lifecycle_stage` overrides the library's value.

#### `model_catalog`

A library-level declaration of the third-party models nodes in the library can use, organized as a `provider → model` registry. Identifiers at both levels are dict keys (the key *is* the stable handle used by node references and admin policies); each entry carries a `display_name` for UI plus optional `terms_url` and `notes`. Each `Model` additionally declares `key_support` (required), an optional `family` grouping tag, and an optional upstream `provider_model_id`.

The `key_support` value tells admins what kind of API key authorizes the call:

| Value                                   | Meaning                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| `REQUIRES_CUSTOMER_KEY`                 | Customer-supplied API key only.                                                     |
| `SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY` | Customer key or Griptape-provided key both work.                                    |
| `REQUIRES_GRIPTAPE_KEY`                 | Griptape-provided key only.                                                         |
| `NO_KEY_REQUIRED`                       | The model runs locally or otherwise needs no API key (e.g. an Ollama-hosted model). |

`notes` (available on both the provider and the model) is free-form author guidance rendered alongside the entry. Use it for caveats that don't fit other fields, like "BYOK requires injecting a provider-specific prompt driver."

```jsonc
{
  "type": "model_catalog",
  "providers": {
    "anthropic": {
      "display_name": "Anthropic",
      "terms_url": "https://www.anthropic.com/legal/commercial-terms",
      "models": {
        "claude_opus_byok": {
          "display_name": "Claude Opus 4 (BYOK)",
          "family": "Claude 4",
          "provider_model_id": "claude-opus-4",
          "key_support": "REQUIRES_CUSTOMER_KEY"
        },
        "claude_opus_griptape": {
          "display_name": "Claude Opus 4 (Griptape Key)",
          "family": "Claude 4",
          "provider_model_id": "claude-opus-4",
          "key_support": "REQUIRES_GRIPTAPE_KEY"
        }
      }
    },
    "kling": {
      "display_name": "Kling",
      "terms_url": "https://app.klingai.com/global/about/terms",
      "models": {
        "kling_v2": {
          "display_name": "Kling v2",
          "provider_model_id": "kling-v2-master",
          "key_support": "REQUIRES_GRIPTAPE_KEY"
        }
      }
    },
    "ollama": {
      "display_name": "Ollama",
      "key_support": "NO_KEY_REQUIRED",
      "notes": "Local runtime; models enumerated at runtime, none declared here."
    }
  }
}
```

A few rules worth knowing:

- **`family` is just a tag.** It clusters related models for display (e.g. the two Claude 4 entries above). It is not a container and is not part of a model's identity, so providers without meaningful families simply omit it.
- **`key_support` lives on the model by default.** Every `Model` declares its own value. The same upstream model with two different key requirements becomes two models under two distinct dict keys (see `claude_opus_byok` and `claude_opus_griptape` above). A `ModelProvider` also accepts an optional `key_support` used only when it declares no models at all (e.g. a local-runtime provider like Ollama where `key_support=NO_KEY_REQUIRED` is the only meaningful signal).
- **Model IDs must be unique across the entire library.** Pydantic enforces uniqueness within each provider's `models` dict for free; collisions across providers are caught at library-load time as `DuplicateModelIdProblem`.
- **At most one `model_catalog` per library.** Declaring two is rejected at validation time; merge their providers into one.

##### `model_usage`

A node references one or more catalog models by their dict keys. Use this when the node binds to a specific, named set of models. Each entry must resolve to a model somewhere in the catalog at library-load time; unresolved references surface as `UnresolvedModelUsageReferenceProblem`.

```jsonc
{ "type": "model_usage", "model_ids": ["claude_opus_byok", "kling_v2"] }
```

##### `model_provider_usage`

A node references one or more entire providers. Use this when a node dynamically enumerates every model a provider offers at runtime. Each entry must resolve to a provider declared in the catalog; unresolved references surface as `UnresolvedModelProviderUsageReferenceProblem`.

```jsonc
{ "type": "model_provider_usage", "provider_ids": ["anthropic", "ollama"] }
```

The two usage declarations are independent. A node can carry any combination — for instance, "every model this provider offers, plus these two specific models from another provider."

#### Combining declarations

A node can carry any combination of declarations. For example, a Labs node that uses two models:

```jsonc
"metadata": {
  "category": "labs",
  "description": "Labs node demonstrating multiple declarations.",
  "display_name": "Labs Node",
  "declarations": [
    { "type": "lifecycle_stage", "stage": "LABS" },
    { "type": "model_usage", "model_ids": ["claude_opus_byok", "kling_v2"] }
  ]
}
```

New declaration types added in future engine releases land additively under this same `declarations` field without a schema-version bump.

## Custom Widget Components

Nodes can use custom JavaScript widget components to provide rich, interactive UI beyond the standard parameter controls. Widgets are standalone `.js` files that render into a container element and communicate value changes back to the framework via a callback.

### Widget Architecture

A custom widget involves three pieces:

1. **Widget JS file** (`widgets/MyWidget.js`) — the UI component
1. **Node Python file** — references the widget via the `Widget` trait on a parameter
1. **Library JSON** (`griptape_nodes_library.json`) — registers the widget so the framework can find it

```
library_name/
├── griptape_nodes_library.json
├── my_node.py
└── widgets/
    └── MyWidget.js
```

### Registering a Widget

Add a `"widgets"` array to `griptape_nodes_library.json`:

```json
{
  "name": "My Library",
  "widgets": [
    {
      "name": "MyWidget",
      "path": "widgets/MyWidget.js",
      "description": "Description of the widget"
    }
  ],
  "nodes": [ ... ]
}
```

The `name` must match the name used in the `Widget` trait on the Python side, and the `library` argument must match the `"name"` field at the top level of the JSON.

### Attaching a Widget to a Parameter

Use the `Widget` trait to bind a parameter to your custom widget. The parameter's value is passed to the widget as `props.value`, and changes flow back through `props.onChange`:

```python
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.traits.widget import Widget

self.add_parameter(
    Parameter(
        name="my_data",
        input_types=["list"],
        type="list",
        output_type="list",
        default_value=[],
        tooltip="Data managed by custom widget",
        allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
        traits={Widget(name="MyWidget", library="My Library")},
    )
)
```

### Widget JS Function Signature

Widgets are ES module default exports. The function receives a container DOM element and a props object, and must return a cleanup function:

```javascript
export default function MyWidget(container, props) {
  const { value, onChange, disabled, height } = props;

  // Build your UI inside `container`
  // Call `onChange(newValue)` when the user changes data
  // Respect `disabled` to prevent interaction when appropriate

  // Return a cleanup function
  return () => {
    // Remove event listeners, dispose resources
  };
}
```

**Props:**

| Prop       | Type       | Description                                      |
| ---------- | ---------- | ------------------------------------------------ |
| `value`    | `any`      | Current parameter value (matches Python default) |
| `onChange` | `function` | Callback to send updated value to the framework  |
| `disabled` | `boolean`  | Whether the widget should be read-only           |
| `height`   | `number`   | Suggested height in pixels (may be 0 or absent)  |

### Critical Patterns and Pitfalls

#### Emit Changes Sparingly — Not on Every Keystroke

Calling `onChange` triggers framework state updates that steal focus from the active element. For text inputs, this means the textarea loses focus after every keystroke, making typing impossible. This is not specific to custom widgets — the built-in `TextComponent` in the Griptape Nodes editor uses the same pattern:

- **Local state** (your internal data array, counters, border colors) updates immediately on every `input` event.
- **`onChange`** is called only on `blur` — when the user clicks away or tabs out of the field.
- **Discrete controls** (buttons, steppers, drag-end) call `onChange` immediately since they don't hold focus.

```javascript
// Local state updates on every keystroke — UI stays responsive
textarea.addEventListener("input", (e) => {
  localData[index].text = e.target.value;
  // Update character counters, border colors, etc. here
});

// Emit to framework only when the user leaves the field
textarea.addEventListener("blur", () => {
  localData[index].text = textarea.value;
  onChange(structuredClone(localData));
});

// Discrete controls (buttons, steppers) can emit immediately
button.addEventListener("pointerdown", (e) => {
  e.stopPropagation();
  localData[index].value++;
  onChange(structuredClone(localData));
  render();
});
```

> **Why not `requestAnimationFrame` to restore focus?** Attempting to call `onChange` on every keystroke and then restore focus via `requestAnimationFrame` does not reliably work — the framework's React rendering cycle can complete asynchronously, and the focus restoration races with it.

#### Prevent Node Drag Interference

Node canvases handle drag events for panning and node movement. Interactive elements inside your widget must stop event propagation and use the `nodrag` / `nowheel` CSS classes:

```javascript
// On the outermost wrapper
const wrapper = document.createElement("div");
wrapper.className = "my-widget nodrag nowheel";

// On interactive child elements (textareas, sliders, etc.)
textarea.addEventListener("pointerdown", (e) => e.stopPropagation());
textarea.addEventListener("mousedown", (e) => e.stopPropagation());
```

#### Prevent Keyboard Shortcut Interference

The node editor binds keyboard shortcuts at the canvas level — for example, pressing `Delete` deletes the selected node. When a text input inside your widget has focus, these shortcuts still fire because keyboard events bubble up. Stop propagation on `keydown` to isolate your text inputs:

```javascript
textarea.addEventListener("keydown", (e) => e.stopPropagation());
```

This prevents the Delete key from deleting the node while the user is editing text, and stops other canvas-level shortcuts (copy, paste, undo at the node level) from interfering with normal text editing.

#### Override `user-select: none` for Text Inputs

Widget wrappers typically set `user-select: none` to prevent accidental text selection during drag operations. This cascades into child elements and blocks textarea editing. Override it explicitly:

```css
textarea {
  user-select: text;
  -webkit-user-select: text;
}
```

#### Clone Values Before Emitting

Always pass a fresh copy to `onChange` — not a reference to your internal state. Otherwise the framework and your widget share the same object, leading to subtle bugs:

```javascript
onChange(localData.map((item) => ({ ...item })));
```

#### Clean Up Document-Level Listeners

If you attach listeners to `document` (e.g., for drag-and-drop or click-outside-to-close), remove them in the cleanup function:

```javascript
document.addEventListener("pointerdown", onDocumentClick, true);

return () => {
  document.removeEventListener("pointerdown", onDocumentClick, true);
};
```

#### Assign Stable IDs to List Items

When building widgets that manage reorderable lists (e.g., drag-and-drop shot editors), give each item a unique ID that is decoupled from array index. Without stable IDs, item attributes such as text field contents can be lost during drag-and-drop reordering because the widget re-renders from scratch and identity was tied to position.

```javascript
let nextItemId = 1;

function assignId(item) {
  if (!item.id) {
    item.id = `item-${nextItemId++}`;
  } else {
    const num = parseInt(item.id.replace("item-", ""), 10);
    if (!isNaN(num) && num >= nextItemId) {
      nextItemId = num + 1;
    }
  }
  return item;
}

// On initialization — preserve existing IDs from saved data
let items = value.map((v) => assignId({ ...v }));

// When adding new items
items.push(assignId({ name: "New Item", text: "" }));
```

The ID persists through reorders, re-renders, and round-trips via `onChange`. The display name (e.g., "Shot1", "Shot2") can be renumbered based on visual position while the `id` remains stable.

#### Handle the `disabled` Attribute Correctly in DOM Helpers

If you write a DOM helper function that creates elements from an attributes object, be careful with the `disabled` attribute. Using `setAttribute("disabled", false)` does **not** remove the disabled state — the presence of the attribute in any form disables the element. Use the property instead:

```javascript
if (key === "disabled") {
  element.disabled = !!val;
}
```

#### Show Drop Indicators at End of List

When implementing drag-and-drop reordering, the drop target indicator (e.g., a blue border) must also appear when dragging past the last item in the list. A common approach: when the drag position is below all items, show a `border-bottom` on the last item instead of a `border-top` on a nonexistent next item:

```javascript
if (dragOverIndex === items.length && item.index === lastIndex) {
  item.el.style.borderBottom = "2px solid #4a9eff";
} else if (item.index === dragOverIndex) {
  item.el.style.borderTop = "2px solid #4a9eff";
}
```

#### Enforce Aggregate Constraints (Min/Max Totals)

When list items have numeric values that must sum to within a range (e.g., total duration 3–15 seconds), enforce the constraint in both directions:

- **Ceiling:** Disable the increase stepper and "add" button when the total would exceed the maximum.
- **Floor:** Disable the decrease stepper when reducing any item would bring the total below the minimum.
- **Auto-compensate on delete:** When removing an item would drop the total below the minimum, increase the last remaining item's value to make up the difference.

```javascript
const MIN_TOTAL = 3;
const MAX_TOTAL = 15;

// Disable decrease if total would go below minimum
const wouldGoBelow = totalValue() - 1 < MIN_TOTAL;
const canDecrease = !disabled && item.value > MIN_VALUE && !wouldGoBelow;

// On delete — auto-compensate to maintain minimum total
trash.addEventListener("pointerdown", (e) => {
  e.stopPropagation();
  if (items.length <= 1) return;
  items.splice(index, 1);
  const total = totalValue();
  if (total < MIN_TOTAL) {
    const lastItem = items[items.length - 1];
    lastItem.value += MIN_TOTAL - total;
  }
  emitChange();
  render();
});
```

Show both bounds in the status bar so users understand the valid range: `"8s (3–15s)"`. Highlight in red when outside bounds.

### Example: List-Based Editor Widget

A common pattern is a widget that manages a list of structured items with add, delete, reorder, and inline editing. Key implementation details:

- **Stable IDs:** Assign a unique `id` to each item that survives reordering and round-trips through `onChange`.
- **Drag-and-drop reordering:** Attach `pointerdown` on drag handles, create a floating clone for visual feedback, track the insertion point via `pointermove`, and finalize the reorder on `pointerup`. Call `onChange` only after the drop. Show drop indicators at both middle and end-of-list positions.
- **Stepper controls:** For constrained numeric values (e.g., duration 1–15s), use ▲/▼ stepper buttons instead of dropdown menus. Disable buttons when they would violate constraints (min/max per item, min/max total across all items).
- **Aggregate constraints:** Enforce both minimum and maximum totals across all items. Auto-compensate on delete to maintain the floor (see [Enforce Aggregate Constraints](#enforce-aggregate-constraints-minmax-totals)).
- **Validation constraints:** Enforce limits (max items, max total values, max text length) by disabling the add button and stepper arrows rather than silently ignoring input.
- **Status feedback:** Show a small status bar with current counts vs. limits (e.g., `"3 / 6 shots"`, `"8s (3–15s)"`) so users understand the valid range and why controls may be disabled.
- **Text input isolation:** Stop propagation on `pointerdown`, `mousedown`, and `keydown` to prevent node drag and keyboard shortcut interference. Emit `onChange` only on `blur`.

```python
# Python side — list parameter with widget
self.add_parameter(
    Parameter(
        name="items",
        input_types=["list"],
        type="list",
        output_type="list",
        default_value=[{"name": "Item1", "duration": 2, "description": ""}],
        allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
        traits={Widget(name="MyListEditor", library="My Library")},
    )
)
```

```javascript
// JS side — skeleton for a list editor widget
export default function MyListEditor(container, props) {
  const { value, onChange, disabled } = props;

  // Stable ID assignment
  let nextId = 1;
  function assignId(item) {
    if (!item.id) item.id = `item-${nextId++}`;
    return item;
  }

  let items = Array.isArray(value)
    ? value.map((v) => assignId({ ...v }))
    : [assignId({ name: "Item1", duration: 2, description: "" })];

  function emitChange() {
    if (!disabled && onChange) {
      onChange(items.map((item) => ({ ...item })));
    }
  }

  function render() {
    container.innerHTML = "";
    const wrapper = document.createElement("div");
    wrapper.className = "nodrag nowheel";

    items.forEach((item, index) => {
      // ... build item row with drag handle, stepper, textarea, trash ...

      // Text input — local update on input, emit on blur
      textarea.addEventListener("input", (e) => {
        items[index].description = e.target.value;
      });
      textarea.addEventListener("blur", () => {
        items[index].description = textarea.value;
        emitChange();
      });

      // Isolate text input from node-level events
      textarea.addEventListener("pointerdown", (e) => e.stopPropagation());
      textarea.addEventListener("mousedown", (e) => e.stopPropagation());
      textarea.addEventListener("keydown", (e) => e.stopPropagation());
    });

    container.appendChild(wrapper);
  }

  render();

  return () => { /* cleanup document-level listeners */ };
}
```

## Contributing to the Standard Library

When adding nodes to the core `griptape_nodes_library` (as opposed to creating a standalone library), follow this process:

### 1. Create a Feature Branch

```bash
cd griptape-nodes
git checkout -b feature/add-color-match-node
```

### 2. Add the Node File

Place your node in the appropriate category subdirectory:

```
libraries/griptape_nodes_library/griptape_nodes_library/
├── image/
│   ├── color_match.py      # New node file
│   ├── load_image.py
│   └── save_image.py
├── text/
├── audio/
└── ...
```

### 3. Update griptape_nodes_library.json

Make three updates to `libraries/griptape_nodes_library/griptape_nodes_library.json`:

#### a. Increment the library version

```json
{
  "metadata": {
    "library_version": "0.59.0" // Was 0.58.0
  }
}
```

#### b. Add any new pip dependencies

```json
{
  "metadata": {
    "dependencies": {
      "pip_dependencies": [
        "existing-dep",
        "color-matcher" // New dependency
      ]
    }
  }
}
```

#### c. Add the node entry

```json
{
  "nodes": [
    {
      "class_name": "ColorMatch",
      "file_path": "griptape_nodes_library/image/color_match.py",
      "metadata": {
        "category": "image",
        "description": "Transfer color characteristics from a reference image to a target image",
        "display_name": "Color Match",
        "icon": "palette",
        "group": "edit"
      }
    }
  ]
}
```

### 4. Add Documentation

Create a documentation page at `docs/nodes/<category>/<node_name>.md`:

```markdown
# Color Match

Transfer color characteristics from a reference image to a target image.

## What It Does

Applies the color palette from a reference image to a target image...

## Parameters

### Inputs

| Parameter       | Type             | Description                 |
| --------------- | ---------------- | --------------------------- |
| reference_image | ImageUrlArtifact | Source of the color palette |
| target_image    | ImageUrlArtifact | Image to apply colors to    |

### Outputs

| Parameter    | Type             | Description          |
| ------------ | ---------------- | -------------------- |
| output_image | ImageUrlArtifact | Color-matched result |

## Example Usage

1. Connect a reference image with desired colors
2. Connect the target image to transform
3. Run the node

## Technical Details

Uses the color-matcher library with histogram matching...
```

### 5. Update mkdocs.yml Navigation

Add your doc page to the navigation in `mkdocs.yml`:

```yaml
nav:
  - Nodes Reference:
      - Image:
          - Load Image: nodes/image/load_image.md
          - Save Image: nodes/image/save_image.md
          - Color Match: nodes/image/color_match.md # New entry
```

### 6. Run Quality Checks

Before committing, run formatting and checks:

```bash
make format        # Auto-format code
make check/lint    # Check for linting issues
make check/types   # Check for type errors
```

Fix any issues that arise before proceeding.

### 7. Commit and Create PR

```bash
git add .
git commit -m "feat(image): add ColorMatch node for color transfer"
git push -u origin HEAD
gh pr create --title "Add ColorMatch node" --body "## Summary
- Adds ColorMatch node for transferring colors between images
- Uses color-matcher library
- Includes documentation

## Test plan
- [ ] Load two images
- [ ] Run color match
- [ ] Verify output has reference colors"
```

### Standard Library vs External Library

| Aspect       | Standard Library                            | External Library                  |
| ------------ | ------------------------------------------- | --------------------------------- |
| Location     | `griptape-nodes` repo                       | Separate repo                     |
| Installation | Included by default                         | User installs                     |
| Review       | Requires PR approval                        | Self-published                    |
| Dependencies | Added to core `griptape_nodes_library.json` | Own `griptape_nodes_library.json` |
| Versioning   | Follows core library version                | Independent versioning            |
| Docs         | Added to main docs site                     | README in library                 |

**When to contribute to standard library:**

- Node has broad utility for many users
- No proprietary/paid API dependencies
- Stable, well-tested implementation
- Follows all code quality standards

**When to create external library:**

- Niche use case
- Requires paid API keys
- Experimental/rapidly changing
- Want independent release cycle

## Appendix

### Imports

```python
# Core imports
from griptape_nodes.exe_types.core_types import (
    Parameter, ParameterList, ParameterMode, ParameterTypeBuiltin,
    ParameterGroup, ParameterMessage, ControlParameterInput, ControlParameterOutput
)
from griptape_nodes.exe_types.node_types import (
    DataNode, ControlNode, BaseNode, SuccessFailureNode,
    StartNode, EndNode
)
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode, BaseIterativeEndNode
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.color_picker import ColorPicker
from griptape_nodes.traits.file_system_picker import FileSystemPicker

# Artifacts
from griptape.artifacts import ImageArtifact, ImageUrlArtifact, TextArtifact

# Utilities
from griptape_nodes_library.utils.artifact_path_tethering import (
    ArtifactPathTethering, ArtifactTetheringConfig
)
from griptape_nodes_library.utils.image_utils import (
    dict_to_image_url_artifact,
    load_pil_from_url,
    save_pil_image_with_named_filename,
)
from griptape_nodes_library.utils.file_utils import generate_filename
```

### Utility Function Reference

#### Image Utilities (`griptape_nodes_library.utils.image_utils`)

| Function                                            | Purpose                                              | Returns            |
| --------------------------------------------------- | ---------------------------------------------------- | ------------------ |
| `dict_to_image_url_artifact(d)`                     | Convert dict representation to ImageUrlArtifact      | `ImageUrlArtifact` |
| `load_pil_from_url(url)`                            | Load PIL Image from URL (handles localhost)          | `PIL.Image.Image`  |
| `save_pil_image_with_named_filename(img, filename)` | Save PIL Image using project system (see note below) | `ImageUrlArtifact` |

**Example usage:**

```python
from griptape_nodes_library.utils.image_utils import (
    dict_to_image_url_artifact,
    load_pil_from_url,
    save_pil_image_with_named_filename,
)

# Convert parameter value to artifact
value = self.get_parameter_value("image")
if isinstance(value, dict):
    artifact = dict_to_image_url_artifact(value)
else:
    artifact = value

# Load as PIL for processing
pil_image = load_pil_from_url(artifact.value)

# Process image...
processed = pil_image.filter(...)

# Save and get output artifact
output_artifact = save_pil_image_with_named_filename(processed, "result.png")
self.parameter_output_values["output"] = output_artifact
```

#### File Utilities (`griptape_nodes_library.utils.file_utils`)

| Function                                    | Purpose                    | Returns |
| ------------------------------------------- | -------------------------- | ------- |
| `generate_filename(node_name, suffix, ext)` | Create consistent filename | `str`   |

**Example usage:**

```python
from griptape_nodes_library.utils.file_utils import generate_filename

# Generate filename like "ColorMatch_processed_abc123.png"
filename = generate_filename(self.name, suffix="processed", ext="png")
```

#### Project System (`griptape_nodes.files.project_file`)

| Class/Function                            | Purpose                                          | Returns                  |
| ----------------------------------------- | ------------------------------------------------ | ------------------------ |
| `ProjectFileDestination.from_situation()` | Create file destination with named situation     | `ProjectFileDestination` |
| `ProjectFileParameter`                    | Parameter component for configurable file output | -                        |

**Example usage:**

```python
from griptape_nodes.files.project_file import ProjectFileDestination

# Save file using project system
dest = ProjectFileDestination.from_situation(
    filename="output.mp4",
    situation="save_node_output"
)
saved = dest.write_bytes(file_bytes)
artifact = VideoUrlArtifact(saved.location)
```

**Note:** The utility functions `save_pil_image_with_named_filename()` and similar helpers use the project system internally. For new code, prefer using `ProjectFileParameter` or `ProjectFileDestination` directly to have full control over file handling. See [Working with the Project System](#working-with-the-project-system) for comprehensive documentation.

### Advanced Parameter Types

- **ControlParameterInput/Output**: For execution flow control
- **ParameterGroup**: For organizing related parameters with collapsible UI
- **ParameterMessage**: For status updates and external links
- **ParameterList**: For accepting multiple inputs of the same type

### Enumerations

- **NodeResolutionState**: UNRESOLVED, RESOLVING, RESOLVED
- **ParameterMode**: INPUT, OUTPUT, PROPERTY
- **ParameterTypeBuiltin**: STR("str"), BOOL("bool"), INT("int"), FLOAT("float"), ANY("any"), NONE("none"), CONTROL_TYPE("parametercontroltype"), ALL("all")

### Advanced Node Types

- **BaseNode**: Most basic node type for custom implementations
- **DataNode**: For data processing without execution flow
- **ControlNode**: For nodes that manage execution flow
- **SuccessFailureNode**: For operations that can succeed or fail
- **BaseIterativeStartNode/BaseIterativeEndNode**: For iterative operations
- **AsyncResult**: For asynchronous processing operations

### Custom Artifacts

Inherit from BaseArtifact and override methods as needed:

```python
from griptape.artifacts import BaseArtifact

class CustomArtifact(BaseArtifact):
    def __init__(self, value: Any, **kwargs):
        super().__init__(value, **kwargs)

    def to_text(self) -> str:
        return str(self.value)
```

### Advanced Lifecycle Methods

#### Spotlight Control

For conditional dependency resolution:

```python
def initialize_spotlight(self) -> None:
    """Custom spotlight initialization - only include evaluate parameter initially."""
    evaluate_param = self.get_parameter_by_name("evaluate")
    if evaluate_param and ParameterMode.INPUT in evaluate_param.get_mode():
        self.current_spotlight_parameter = evaluate_param

def advance_parameter(self) -> bool:
    """Custom parameter advancement with conditional dependency resolution."""
    if self.current_spotlight_parameter is None:
        return False

    # Special handling for conditional parameters
    if self.current_spotlight_parameter is self.evaluate:
        try:
            evaluation_result = self.check_evaluation()
            next_param = self.output_if_true if evaluation_result else self.output_if_false

            if ParameterMode.INPUT in next_param.get_mode():
                self.current_spotlight_parameter.next = next_param
                next_param.prev = self.current_spotlight_parameter
                self.current_spotlight_parameter = next_param
                return True
        except Exception:
            self.current_spotlight_parameter = None
            return False

    return super().advance_parameter()
```

#### Control Flow Management

```python
def get_next_control_output(self) -> Parameter | None:
    """Return the appropriate control output based on evaluation."""
    if "evaluate" not in self.parameter_output_values:
        self.stop_flow = True
        return None

    if self.parameter_output_values["evaluate"]:
        return self.get_parameter_by_name("Then")
    return self.get_parameter_by_name("Else")
```

### Widget Testbed

The **widget-testbed** is a standalone React + Vite application for testing and developing custom widget components outside the full Griptape Nodes environment. It provides a lightweight, hot-reloading development environment where you can iterate quickly on widget UI and behavior.

#### Purpose

Custom widgets for Griptape Nodes are imperative JavaScript functions that manage their own DOM and state. The widget-testbed allows you to:

- **Rapid prototyping**: Test widget behavior with instant hot-reload during development
- **Isolated testing**: Work on widget UI/UX without launching the full Griptape Nodes application
- **State management verification**: Test complex state transitions and user interactions
- **Cross-widget development**: Easily switch between testing different widgets
- **Debug UI issues**: Inspect the widget's rendered output and state in a clean environment

#### When to Use

Use the widget-testbed when:

- Creating a new custom widget component from scratch
- Debugging widget behavior issues (focus loss, drag-and-drop, event handling)
- Testing widget state management and `onChange` callback patterns
- Verifying widget appearance and layout without node editor interference
- Developing widgets that manage complex internal state (lists, editors, multi-step forms)

#### File Structure

```
widget-testbed/
├── index.html              # Entry HTML with minimal styling
├── package.json            # Dependencies (React 19, Vite 6)
├── vite.config.js          # Vite configuration with React plugin
├── src/
│   ├── main.jsx            # React app entry point
│   ├── App.jsx             # Main test harness with controls
│   └── WidgetHost.jsx      # React wrapper for imperative widgets
└── node_modules/           # Dependencies
```

#### Key Components

##### WidgetHost.jsx

The `WidgetHost` component is a React wrapper that hosts imperative widget functions using the same `(container, props)` signature as Griptape Nodes widgets. It handles the lifecycle of mounting, updating, and unmounting widgets while preventing unnecessary re-renders.

**Key features:**

- **Imperative widget support**: Calls your widget function with a container element and props
- **Smart re-mounting**: Only re-mounts the widget when value changes externally (e.g., Reset button)
- **onChange differentiation**: Tracks whether changes originated from the widget or parent
- **Cleanup management**: Properly calls widget cleanup functions on unmount or re-mount

**Props:**

| Prop       | Type       | Description                                                 |
| ---------- | ---------- | ----------------------------------------------------------- |
| `widgetFn` | `function` | The widget function to render (container, props) => cleanup |
| `value`    | `any`      | Current widget value                                        |
| `onChange` | `function` | Callback when widget emits changes                          |
| `disabled` | `boolean`  | Whether widget should be read-only (default: false)         |
| `height`   | `number`   | Suggested height in pixels (default: 0)                     |

**Implementation pattern:**

```javascript
import WidgetHost from "./WidgetHost";
import MyWidget from "../../path/to/widgets/MyWidget.js";

export default function App() {
  const [value, setValue] = useState(initialValue);
  const [disabled, setDisabled] = useState(false);

  return (
    <WidgetHost
      widgetFn={MyWidget}
      value={value}
      onChange={setValue}
      disabled={disabled}
    />
  );
}
```

##### App.jsx

The main test harness that provides:

- **Widget mounting**: Imports and renders the widget via `WidgetHost`
- **State controls**: Checkbox to toggle disabled state
- **Debug panel**: JSON view of current widget state (toggle with checkbox)
- **Reset functionality**: Button to reset widget to initial state
- **Visual layout**: Clean, dark-themed UI matching Griptape Nodes aesthetic

#### Testing a Widget

**1. Install dependencies:**

```bash
cd widget-testbed
npm install
```

**2. Update App.jsx to import your widget:**

```javascript
import MyWidget from "../../my-library/widgets/MyWidget.js";

const INITIAL_VALUE = { /* your initial state */ };

export default function App() {
  const [value, setValue] = useState(INITIAL_VALUE);
  const [disabled, setDisabled] = useState(false);
  const [showDebug, setShowDebug] = useState(true);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <h1 style={{ fontSize: 18, fontWeight: 600, color: "#eee" }}>
        MyWidget Testbed
      </h1>

      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={disabled}
            onChange={(e) => setDisabled(e.target.checked)}
          />
          Disabled
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={showDebug}
            onChange={(e) => setShowDebug(e.target.checked)}
          />
          Show JSON
        </label>
        <button
          onClick={() => setValue(INITIAL_VALUE)}
          style={{
            padding: "4px 12px",
            fontSize: 12,
            background: "#333",
            border: "1px solid #555",
            borderRadius: 4,
            color: "#ccc",
            cursor: "pointer",
          }}
        >
          Reset
        </button>
      </div>

      <div
        style={{
          border: "1px solid #333",
          borderRadius: 8,
          overflow: "hidden",
        }}
      >
        <WidgetHost
          widgetFn={MyWidget}
          value={value}
          onChange={setValue}
          disabled={disabled}
        />
      </div>

      {showDebug && (
        <pre
          style={{
            background: "#1a1a1a",
            border: "1px solid #333",
            borderRadius: 8,
            padding: 12,
            fontSize: 11,
            color: "#8c8",
            overflow: "auto",
            maxHeight: 300,
          }}
        >
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}
```

**3. Start the development server:**

```bash
npm run dev
```

**4. Open in browser:**

Navigate to `http://localhost:5173` (or the port shown in terminal).

#### Development Workflow

**Typical development cycle:**

1. **Write widget code**: Create or modify your widget `.js` file
1. **Update testbed**: Import the widget in `App.jsx`
1. **Run dev server**: `npm run dev` for hot-reload
1. **Test interactions**: Click, type, drag, and interact with the widget
1. **Verify state**: Check the JSON debug panel to see state changes
1. **Test edge cases**: Use Reset button and Disabled toggle to test edge cases
1. **Iterate**: Make changes to widget code and see updates instantly

**Common testing scenarios:**

- **Focus management**: Type in text fields, ensure focus isn't lost on `onChange`
- **Drag-and-drop**: Test reordering, ensure item identity is preserved
- **State transitions**: Add/remove items, verify correct state updates
- **Disabled mode**: Toggle disabled, ensure widget becomes read-only
- **External state changes**: Use Reset button to verify widget handles external updates
- **Event propagation**: Ensure clicks/drags don't interfere with parent (use `nodrag` class)
- **Keyboard shortcuts**: Test that Delete, Ctrl+C, etc. don't trigger node-level actions

#### WidgetHost Pattern Details

The `WidgetHost` component solves a critical problem: **preventing unnecessary widget re-mounts when the widget itself triggers `onChange`**. Without this, the widget would be destroyed and recreated on every keystroke, losing focus and internal state.

**How it works:**

1. **Flag-based change tracking**: `isWidgetChangeRef` tracks whether the current change originated from the widget
1. **Conditional re-mount**: Widget is only re-mounted when `value` changes externally (not from `onChange`)
1. **Stable onChange callback**: Uses `useCallback` to prevent unnecessary effect triggers
1. **Cleanup on unmount**: Calls widget's cleanup function when widget is destroyed or value changes externally

**Key implementation:**

```javascript
const isWidgetChangeRef = useRef(false);

const stableOnChange = useCallback(
  (newValue) => {
    isWidgetChangeRef.current = true;  // Mark as widget-originated change
    onChange?.(newValue);
  },
  [onChange],
);

useEffect(() => {
  if (isWidgetChangeRef.current) {
    isWidgetChangeRef.current = false;  // Clear flag and skip re-mount
    return;
  }

  // External value change: re-mount widget
  const cleanup = widgetFn(container, { value, onChange: stableOnChange, disabled, height });
  return cleanup;
}, [widgetFn, value, disabled, height, stableOnChange]);
```

This pattern ensures the widget maintains its internal DOM and state across `onChange` calls, preventing focus loss and other re-mount issues.

#### Best Practices

**When using the widget-testbed:**

- **Match production props**: Use the same prop names (`value`, `onChange`, `disabled`, `height`) as Griptape Nodes
- **Test disabled state**: Always verify your widget respects the `disabled` prop
- **Verify cleanup**: Check that your widget's cleanup function properly removes event listeners
- **Test edge cases**: Use the Reset button to test how your widget handles external value changes
- **Inspect state**: Keep the JSON debug panel visible to understand state flow
- **Test keyboard events**: Ensure `stopPropagation` on `keydown` prevents node-level shortcuts
- **Test mouse events**: Ensure `stopPropagation` on `pointerdown`/`mousedown` prevents node dragging
- **Verify cloning**: Check that `onChange` receives cloned data, not references to internal state

**Don't:**

- Don't commit `widget-testbed/` to your library repository (it's a development tool)
- Don't test production-specific features (node connections, workflow execution)
- Don't assume testbed behavior matches production exactly (always final-test in Griptape Nodes)

#### Example: MultiShotEditor Testbed

The current testbed configuration tests the `MultiShotEditor` widget from the Kling library:

```javascript
import MultiShotEditor from "../../kling/widgets/MultiShotEditor.js";

const INITIAL_SHOTS = [{ name: "Shot1", duration: 2, description: "" }];

export default function App() {
  const [shots, setShots] = useState(INITIAL_SHOTS);
  // ... controls and debug UI ...

  return (
    <WidgetHost
      widgetFn={MultiShotEditor}
      value={shots}
      onChange={setShots}
      disabled={disabled}
    />
  );
}
```

This demonstrates the testbed pattern for a complex widget managing an array of shot objects with drag-and-drop reordering, add/remove functionality, and multiple text inputs.

## Asynchronous API Integration

### Process Method with Yield Syntax

For nodes that perform long-running asynchronous operations, use the `yield` syntax to properly handle async processing:

```python
from griptape_nodes.exe_types.node_types import DataNode, AsyncResult

class MyAsyncNode(DataNode):
    def process(self) -> AsyncResult | None:
        """Process the request asynchronously."""
        yield lambda: self._process()

    def _process(self) -> None:
        """Main processing method."""
        try:
            # Set safe defaults
            self._set_safe_defaults()

            # Validate API key
            api_key = self._validate_api_key()

            # Submit task
            task_id = self._submit_task(api_key)

            # Poll for completion
            result = self._poll_for_completion(task_id, api_key)

            # Process result
            self.parameter_output_values["output"] = result

        except Exception as e:
            self._set_safe_defaults()
            self._log(f"Processing failed: {e}")
            raise RuntimeError(f"{self.name}: {str(e)}") from e
```

**Key Points:**

- `process()` returns `AsyncResult | None` and yields a lambda
- Actual work is done in `_process()` method
- Pattern matches Minimax and other async nodes
- Enables proper async handling in the workflow engine

### Polling Pattern for Long-Running Tasks

When integrating with APIs that use asynchronous task processing (video generation, model training, etc.), implement a three-step pattern:

#### Step 1: Task Submission

```python
def _submit_task(self, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Submit task and return response with task_id."""
    payload = self._build_payload(params)

    response = requests.post(
        self.API_BASE_URL,
        json=payload,
        headers=headers,
        timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()

    response_data = response.json()
    task_id = response_data.get("task_id")
    return response_data
```

#### Step 2: Status Polling

```python
POLLING_INTERVAL = 10  # seconds (use API-recommended value)
MAX_POLLING_ATTEMPTS = 60  # 10 minutes max

def _poll_for_completion(self, task_id: str, headers: dict[str, str]) -> str | None:
    """Poll API for task completion and return result identifier."""
    query_url = "https://api.example.com/v1/query/task"

    for attempt in range(MAX_POLLING_ATTEMPTS):
        time.sleep(POLLING_INTERVAL)  # Wait before each poll

        response = requests.get(
            query_url,
            headers=headers,
            params={"task_id": task_id},  # Use query params, not path
            timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()

        status_data = response.json()
        status = status_data.get("status")

        self._log(f"Polling attempt {attempt + 1}: Status = {status}")

        if status == "Success":
            file_id = status_data.get("file_id")
            return file_id
        elif status == "Fail":
            error_msg = status_data.get("error_message", "Unknown error")
            raise RuntimeError(f"Task failed: {error_msg}")
        # Continue polling for "Processing", "Pending", etc.

    raise RuntimeError(f"Task did not complete within {MAX_POLLING_ATTEMPTS * POLLING_INTERVAL} seconds")
```

#### Step 3: Result Retrieval

```python
def _retrieve_result(self, file_id: str, headers: dict[str, str]) -> str:
    """Retrieve download URL from result identifier."""
    retrieve_url = "https://api.example.com/v1/files/retrieve"

    response = requests.get(
        retrieve_url,
        headers=headers,
        params={"file_id": file_id},
        timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()

    response_data = response.json()
    download_url = response_data.get("file", {}).get("download_url")

    return download_url
```

**Key Considerations:**

- Always use API-recommended polling intervals (typically 5-10 seconds)
- Set reasonable maximum attempts to prevent infinite loops
- Use query parameters, not path parameters, for task_id (verify with API docs)
- Handle all status states: Success, Fail, Processing, Pending
- Log polling attempts for debugging
- Set safe defaults on failure

### Dynamic Endpoint Selection Based on Inputs

When a node can operate in multiple modes depending on which inputs are connected (e.g., image-to-video when images are provided, text-to-video when they are not), select the API endpoint dynamically in the process method rather than hardcoding a single URL:

```python
IMAGE2VIDEO_URL = "https://api.example.com/v1/videos/image2video"
TEXT2VIDEO_URL = "https://api.example.com/v1/videos/text2video"

def _process(self):
    image_data = self._get_image_data("start_frame")
    has_images = image_data is not None

    if has_images:
        api_url = IMAGE2VIDEO_URL
    else:
        api_url = TEXT2VIDEO_URL

    payload = self._build_payload()
    if image_data:
        payload["image"] = image_data

    response = requests.post(api_url, headers=headers, json=payload, timeout=30)
    # ... polling uses the same api_url for status checks
    poll_url = f"{api_url}/{task_id}"
```

This avoids requiring image inputs when the user wants text-only generation, and ensures the correct API endpoint is called for each mode. The polling URL should use the same base endpoint.

### Image Artifact Conversion to Base64

**CRITICAL: Localhost URL Handling**

When sending images to external APIs, ImageUrlArtifact URLs from static storage are localhost and inaccessible to external services. Always detect and convert localhost URLs to base64:

```python
import base64

def _get_image_data(self, image_artifact: ImageArtifact | ImageUrlArtifact) -> str:
    """Convert image artifact to URL or base64 data URI."""

    # ImageUrlArtifact - check if localhost or public URL
    if isinstance(image_artifact, ImageUrlArtifact):
        url = image_artifact.value

        # Localhost URLs must be converted to base64 for external APIs
        if url.startswith(('http://localhost', 'http://127.0.0.1',
                          'https://localhost', 'https://127.0.0.1')):
            self._log(f"Converting localhost URL to base64: {url[:100]}...")
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            image_bytes = response.content

            # Detect MIME type from headers
            mime_type = response.headers.get('content-type', 'image/jpeg')
            if not mime_type.startswith('image/'):
                mime_type = 'image/jpeg'

            base64_data = base64.b64encode(image_bytes).decode('utf-8')
            return f"data:{mime_type};base64,{base64_data}"

        # Public URLs can be passed through
        self._log(f"Using public URL: {url[:100]}...")
        return url

    # ImageArtifact - use .base64 property (preferred method)
    if isinstance(image_artifact, ImageArtifact):
        # PREFERRED: Use built-in properties
        if hasattr(image_artifact, 'base64') and hasattr(image_artifact, 'mime_type'):
            base64_data = image_artifact.base64  # Raw base64 (no prefix)
            mime_type = image_artifact.mime_type  # e.g., 'image/jpeg'

            # Check if already has data URI prefix
            if base64_data.startswith('data:'):
                self._log("Using ImageArtifact.base64 (already has data URI)")
                return base64_data

            # Add data URI prefix
            self._log(f"Using ImageArtifact.base64 with mime_type: {mime_type}")
            return f"data:{mime_type};base64,{base64_data}"

        # FALLBACK: Manual byte extraction
        self._log("Falling back to manual base64 encoding")
        if hasattr(image_artifact, 'value') and hasattr(image_artifact.value, 'read'):
            image_artifact.value.seek(0)
            image_bytes = image_artifact.value.read()
        elif hasattr(image_artifact, 'data'):
            if isinstance(image_artifact.data, bytes):
                image_bytes = image_artifact.data
            elif hasattr(image_artifact.data, 'read'):
                image_artifact.data.seek(0)
                image_bytes = image_artifact.data.read()
            else:
                raise ValueError("Unsupported ImageArtifact format")
        else:
            raise ValueError("Unsupported ImageArtifact format")

        # Detect MIME type with PIL
        mime_type = "image/jpeg"
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))
            format_to_mime = {
                'JPEG': 'image/jpeg',
                'PNG': 'image/png',
                'WEBP': 'image/webp'
            }
            mime_type = format_to_mime.get(img.format, 'image/jpeg')
        except Exception:
            pass

        base64_data = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:{mime_type};base64,{base64_data}"

    raise ValueError("Unsupported artifact type")
```

**Key Points:**

1. **Always detect localhost URLs** - External APIs cannot access them
1. **Use ImageArtifact.base64 property** - The proper Griptape way (returns raw base64)
1. **Use ImageArtifact.mime_type property** - Automatic MIME type detection
1. **Log which path is used** - Essential for debugging
1. **Download localhost files** - Convert to base64 before sending to API

**Parameter Definition:**

```python
Parameter(
    name="image_input",
    input_types=["ImageArtifact", "ImageUrlArtifact"],  # Accept both
    type="ImageArtifact",
    tooltip="Image input (file or URL)",
    ui_options={"clickable_file_browser": True},  # Enable file browser
)
```

### Multi-Image Input Validation

When nodes accept multiple image parameters, use a reusable validation method with clear parameter identification:

```python
def _validate_image(self, image_artifact: ImageArtifact | ImageUrlArtifact,
                    param_name: str) -> list[Exception]:
    """Validate image with parameter name in error messages."""
    exceptions = []

    if isinstance(image_artifact, ImageArtifact):
        # Get image bytes
        if hasattr(image_artifact, 'value') and hasattr(image_artifact.value, 'read'):
            image_artifact.value.seek(0)
            image_bytes = image_artifact.value.read()
            image_artifact.value.seek(0)
        else:
            return exceptions

        # Validate size
        size_mb = len(image_bytes) / (1024 * 1024)
        if size_mb >= 20:
            exceptions.append(ValueError(
                f"{self.name}: {param_name} size must be < 20MB (current: {size_mb:.1f}MB)"
            ))

        # Validate format and dimensions
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))

            if img.format not in ['JPEG', 'PNG', 'WEBP']:
                exceptions.append(ValueError(
                    f"{self.name}: {param_name} format must be JPG, PNG, or WebP (current: {img.format})"
                ))

            width, height = img.size
            short_edge = min(width, height)
            if short_edge <= 300:
                exceptions.append(ValueError(
                    f"{self.name}: {param_name} short edge must be > 300px (current: {short_edge}px)"
                ))
        except ImportError:
            self._log("PIL not available for validation")
        except Exception as e:
            self._log(f"Error validating {param_name}: {e}")

    return exceptions

def validate_before_node_run(self) -> list[Exception] | None:
    """Validate all image parameters."""
    exceptions = []

    # Validate each image parameter independently
    first_frame = self.get_parameter_value("first_frame_image")
    if first_frame:
        exceptions.extend(self._validate_image(first_frame, "first_frame_image"))

    last_frame = self.get_parameter_value("last_frame_image")
    if last_frame:
        exceptions.extend(self._validate_image(last_frame, "last_frame_image"))

    return exceptions if exceptions else None
```

**Benefits:**

- Clear error messages identifying which image parameter has issues
- Reusable validation logic across multiple image inputs
- Independent validation for each parameter
- Actionable feedback for users

### Model-Dependent Parameter Management

When different models support different parameter combinations:

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    """Handle model-dependent parameter visibility and options."""
    if parameter.name == "model":
        if value == "AdvancedModel":
            # Show model-specific parameters
            self.show_parameter_by_name("advanced_option")

            # Update dropdown choices dynamically
            resolution_param = self.get_parameter_by_name("resolution")
            if resolution_param:
                for child in resolution_param.children:
                    if hasattr(child, 'choices'):
                        child.choices = ADVANCED_MODEL_RESOLUTIONS
                        break
        else:
            # Hide and reset for other models
            self.hide_parameter_by_name("advanced_option")

            # Update to standard choices
            resolution_param = self.get_parameter_by_name("resolution")
            if resolution_param:
                for child in resolution_param.children:
                    if hasattr(child, 'choices'):
                        child.choices = STANDARD_RESOLUTIONS
                        break
                self.set_parameter_value("resolution", "720P")

    return super().after_value_set(parameter, value)
```

**Model-Specific Validation:**

```python
def validate_before_node_run(self) -> list[Exception] | None:
    """Validate model-specific parameter combinations."""
    exceptions = []

    model = self.get_parameter_value("model")
    duration = self.get_parameter_value("duration")
    resolution = self.get_parameter_value("resolution")

    # Example: 10s only for specific model/resolution
    if duration == 10:
        if model != "AdvancedModel":
            exceptions.append(ValueError(f"{self.name}: 10s duration only supported by AdvancedModel"))
        elif resolution == "4K":
            exceptions.append(ValueError(f"{self.name}: 10s duration not supported with 4K resolution"))

    # Model-specific parameter requirements
    if model in ["ModelB", "ModelC"]:
        required_param = self.get_parameter_value("required_for_model_b_c")
        if not required_param:
            exceptions.append(ValueError(f"{self.name}: Parameter required for {model}"))

    return exceptions if exceptions else None
```

### Deprecated Model Migration and User Notification

When a model provider deprecates endpoints (e.g., preview models replaced by GA equivalents), nodes should automatically migrate saved workflows while informing the user. This pattern uses three components working together:

1. A `DEPRECATED_MODELS` dictionary mapping old model names to their replacements
1. A hidden `ParameterMessage` element that acts as a dismissable info banner
1. The `before_value_set` lifecycle hook to intercept and replace deprecated values before they are applied

**Step 1: Define the deprecation map and current models**

```python
from griptape_nodes.exe_types.core_types import Parameter, ParameterMessage
from griptape_nodes.traits.button import Button

MODELS = [
    "veo-3.1-generate-001",
    "veo-3.1-fast-generate-001",
]

# Mapping of deprecated model names to their replacements.
# When a saved workflow references one of these, the node auto-migrates.
DEPRECATED_MODELS: dict[str, str] = {
    "veo-3.1-generate-preview": "veo-3.1-generate-001",
    "veo-3.1-fast-generate-preview": "veo-3.1-fast-generate-001",
    "veo-3.0-generate-001": "veo-3.1-generate-001",
    "veo-2.0-generate-001": "veo-3.1-generate-001",
}
```

**Step 2: Add a hidden ParameterMessage in `__init__`**

Place this after the model parameter so it appears near the model selector in the UI. The `hide=True` keeps it invisible until needed. The `Button` trait with `on_click` gives the user a "Dismiss" button.

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # ... model parameter added above ...

    # Hidden deprecation notice — shown when a deprecated model is detected
    self.add_node_element(
        ParameterMessage(
            name="model_deprecation_notice",
            title="Model Deprecation Notice",
            variant="info",
            value="",
            traits={
                Button(
                    full_width=True,
                    on_click=lambda _, __: self.hide_message_by_name("model_deprecation_notice"),
                )
            },
            button_text="Dismiss",
            hide=True,
        )
    )
```

**Step 3: Implement `before_value_set` to intercept deprecated models**

`before_value_set` fires before the parameter's value is applied. This is the right place to swap a deprecated model for its replacement, because `after_value_set` (and any logic that depends on the model value) will see the replacement.

```python
def before_value_set(self, parameter: Parameter, value: Any) -> Any:
    """Auto-migrate deprecated models and show a deprecation notice."""
    if parameter.name == "model" and value in DEPRECATED_MODELS:
        replacement = DEPRECATED_MODELS[value]
        message = self.get_message_by_name_or_element_id("model_deprecation_notice")
        if message is not None:
            message.value = (
                f"The '{value}' model has been deprecated. "
                f"The model has been updated to '{replacement}'. "
                "Please save your workflow to apply this change."
            )
            self.show_message_by_name("model_deprecation_notice")
        value = replacement

    return super().before_value_set(parameter, value)
```

**Step 4: Hide the notice when the user selects a valid model**

In `after_value_set`, dismiss the banner when the current model is not deprecated. This handles the case where the user manually selects a different model after the migration.

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    if parameter.name == "model":
        # ... model-specific logic (update duration choices, etc.) ...
        if value not in DEPRECATED_MODELS:
            self.hide_message_by_name("model_deprecation_notice")

    return super().after_value_set(parameter, value)
```

**How it works end-to-end:**

1. A user opens a workflow saved with `"veo-3.1-generate-preview"`.
1. The framework calls `before_value_set` with the saved value.
1. The hook detects it in `DEPRECATED_MODELS`, swaps it to `"veo-3.1-generate-001"`, and shows the info banner.
1. `after_value_set` fires with the replacement value — model-dependent UI updates (duration choices, parameter visibility, etc.) work correctly because they see the valid GA model.
1. The user sees the banner: *"The 'veo-3.1-generate-preview' model has been deprecated. The model has been updated to 'veo-3.1-generate-001'. Please save your workflow to apply this change."*
1. The user can dismiss the banner or it hides automatically on the next valid model selection.

**Key API methods used:**

| Method                                         | Purpose                                           |
| ---------------------------------------------- | ------------------------------------------------- |
| `self.add_node_element(ParameterMessage(...))` | Adds the message element to the node              |
| `self.get_message_by_name_or_element_id(name)` | Retrieves the message element to update its value |
| `self.show_message_by_name(name)`              | Makes the hidden message visible                  |
| `self.hide_message_by_name(name)`              | Hides the message again                           |

**Reference implementations:**

- `GriptapeCloudPrompt` in `griptape_nodes_library/config/prompt/griptape_cloud_prompt.py` (standard library)
- `VeoVideoGenerator`, `VeoImageToVideoGenerator`, `VeoTextToVideoWithRef` in the `griptape-nodes-library-googleai` external library

### Enhanced Debug Logging for API Integration

For nodes that integrate with external APIs, implement comprehensive debug logging to quickly diagnose issues:

```python
# Task Submission - Log full response
def _submit_task(self, params: dict, headers: dict) -> dict:
    response = requests.post(API_URL, json=payload, headers=headers)
    response.raise_for_status()

    response_data = response.json()
    self._log(f"Task submission response: {json.dumps(response_data, indent=2)}")
    return response_data

# Payload Sizes - Log data sizes before sending
def _log_request(self, payload: dict) -> None:
    if "first_frame_image" in payload:
        img_len = len(payload.get("first_frame_image", ""))
        self._log(f"first_frame_image data length: {img_len} chars (~{img_len/1024:.1f}KB)")

    if "last_frame_image" in payload:
        img_len = len(payload.get("last_frame_image", ""))
        self._log(f"last_frame_image data length: {img_len} chars (~{img_len/1024:.1f}KB)")

# Error Responses - Log full API error details
def _poll_for_completion(self, task_id: str, headers: dict) -> str:
    status_data = response.json()
    status = status_data.get("status")

    if status == "Fail":
        # Log complete error response for debugging
        self._log(f"Full API error response: {json.dumps(status_data, indent=2)}")
        error_msg = status_data.get("error_message", "Unknown error")
        raise RuntimeError(f"Task failed: {error_msg}")

# Processing Paths - Log which code path is executed
def _get_image_data(self, image_artifact) -> str:
    if isinstance(image_artifact, ImageUrlArtifact):
        if url.startswith('http://localhost'):
            self._log(f"Converting localhost URL to base64: {url[:100]}...")
        else:
            self._log(f"Using public URL: {url[:100]}...")
    elif isinstance(image_artifact, ImageArtifact):
        if hasattr(image_artifact, 'base64'):
            self._log(f"Using ImageArtifact.base64 with mime_type: {mime_type}")
        else:
            self._log("Falling back to manual base64 encoding")
```

**What to Log:**

- **Full API responses** (submission, polling, retrieval)
- **Payload sizes** (especially for base64 data)
- **Processing paths** (which code branches execute)
- **Model/parameter combinations** being used
- **Error details** (full error response from API)

**Benefits:**

- Quickly identify where failures occur
- Understand what data is being sent
- Track which code paths execute
- Get exact API error messages and codes
- Debug without reproducing issues

### API Documentation Verification

**Critical Best Practice:** Always verify API specifications directly from documentation.

**Common Pitfalls to Avoid:**

1. **Model Names**: Check exact capitalization (`MiniMax-Hailuo-02` not `video-01`)
1. **Endpoints**: Verify exact URLs (`/v1/query/video_generation` not `/v1/video_generation/{id}`)
1. **Parameters**: Check query params vs path params
1. **Response Structure**: Verify exact field names (`file_id` vs `file_list`)
1. **Polling Intervals**: Use API-recommended values

**Example: Correct vs Incorrect Polling:**

```python
# ✅ CORRECT: Query parameter
response = requests.get(
    "https://api.example.com/v1/query/task",
    params={"task_id": task_id}
)

# ❌ INCORRECT: Path parameter (unless API specifies this)
response = requests.get(
    f"https://api.example.com/v1/query/task/{task_id}"
)
```

**When Documentation is Inaccessible:**

- Explicitly state inability to access web pages (e.g., JavaScript-heavy docs)
- Request user to provide relevant documentation sections
- Never assume or infer API patterns without verification
- Update implementation when code samples are provided

### Library Structure with uv Dependency Management

**Modern Approach**: Use `uv` for fast, reproducible dependency management following the Minimax library pattern.

#### Directory Structure

```
library-name/
├── pyproject.toml              # uv configuration
├── uv.lock                     # Lock file (generated)
├── LICENSE                     # License file
├── README.md                   # Documentation
├── CHANGELOG.md                # Version history
├── .gitignore                  # Ignore rules
└── library_name/
    ├── griptape_nodes_library.json  # Library metadata
    └── node_file.py
```

#### pyproject.toml Configuration

```toml
[project]
name = "library-name"
version = "1.0.0"
description = "Description of your library"
authors = [
    {name = "Your Name", email = "email@example.com"}
]
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "griptape-nodes-engine",
    "requests",
    # Add other dependencies
]

[tool.uv.sources]
griptape-nodes-engine = { git = "https://github.com/griptape-ai/griptape-nodes", rev="latest"}

[tool.hatch.build.targets.wheel]
packages = ["library_name"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

#### Library Configuration (inside subdirectory)

Place `griptape_nodes_library.json` inside the library subdirectory:

```json
{
  "name": "Library Name",
  "library_schema_version": "0.1.0",
  "settings": [
    {
      "description": "API keys required by nodes",
      "category": "app_events.on_app_initialization_complete",
      "contents": {
        "secrets_to_register": ["API_KEY_NAME"]
      }
    }
  ],
  "nodes": [
    {
      "class_name": "NodeClassName",
      "file_path": "node_file.py", // Relative to library subdirectory
      "metadata": {
        "category": "category_name",
        "description": "Node description",
        "display_name": "Node Display Name"
      }
    }
  ]
}
```

#### Installation Instructions in README

Provide both uv (recommended) and pip (fallback) installation methods:

````markdown
## Installation

### Option 1: Using uv (Recommended)

1. Clone or download this library
2. Install dependencies:
   ```bash
   cd library-name
   uv sync
   ```
````

1. Place in Griptape Nodes libraries directory

### Option 2: Automatic Installation

1. Place folder in libraries directory
1. Dependencies install automatically via pip

````

#### Generate Lock File

```bash
cd library-name
uv sync
````

**Benefits:**

- Fast installation (Rust-based)
- Reproducible builds via lock file
- Direct GitHub integration for griptape-nodes
- Backward compatible with pip installation

### Two-Mode UI Pattern (Simple + Custom)

**Use Case**: Create beginner-friendly nodes while offering advanced control for power users.

**Example**: Music/video generation APIs often have "simple description" mode and "detailed control" mode.

#### Implementation Pattern

```python
class GenerativeNode(DataNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Mode selector
        mode_param = Parameter(
            name="custom_mode",
            input_types=["bool"],
            type="bool",
            default_value=False,
            tooltip="Custom Mode: Full control. Simple Mode: Auto-generate from prompt.",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"display_name": "Custom Mode"},
        )
        self.add_parameter(mode_param)

        # Prompt (meaning changes by mode)
        prompt_param = Parameter(
            name="prompt",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip=[
                {"type": "text", "text": "Custom Mode: Exact lyrics/script"},
                {"type": "text", "text": "Simple Mode: General description"},
            ],
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"multiline": True, "display_name": "Prompt"},
        )
        self.add_parameter(prompt_param)

        # Advanced parameters (custom mode only)
        style_param = Parameter(
            name="style",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip="Style/genre (Custom Mode only)",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"hide": True},  # Hidden by default
        )
        self.add_parameter(style_param)

        title_param = Parameter(
            name="title",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip="Title (Custom Mode only)",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"hide": True},  # Hidden by default
        )
        self.add_parameter(title_param)

        # Initialize visibility
        self._initialize_parameter_visibility()

    def _initialize_parameter_visibility(self) -> None:
        """Initialize parameter visibility based on default mode."""
        custom_mode = self.get_parameter_value("custom_mode") or False
        if custom_mode:
            self.show_parameter_by_name("style")
            self.show_parameter_by_name("title")
        else:
            self.hide_parameter_by_name("style")
            self.hide_parameter_by_name("title")

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Update UI based on mode selection."""
        if parameter.name == "custom_mode":
            if value:
                self.show_parameter_by_name("style")
                self.show_parameter_by_name("title")
            else:
                self.hide_parameter_by_name("style")
                self.hide_parameter_by_name("title")

        return super().after_value_set(parameter, value)

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate based on selected mode."""
        exceptions = []
        custom_mode = self.get_parameter_value("custom_mode")

        if custom_mode:
            # Custom mode requires style and title
            style = self.get_parameter_value("style") or ""
            title = self.get_parameter_value("title") or ""

            if not style.strip():
                exceptions.append(ValueError(f"{self.name}: Style required in Custom Mode"))
            if not title.strip():
                exceptions.append(ValueError(f"{self.name}: Title required in Custom Mode"))
        else:
            # Simple mode just needs prompt
            prompt = self.get_parameter_value("prompt") or ""
            if not prompt.strip():
                exceptions.append(ValueError(f"{self.name}: Prompt required in Simple Mode"))

        return exceptions if exceptions else None
```

**Best Practice for First-Time Users**: Default to Simple Mode with recommendation in documentation:

```markdown
### Getting Started

#### Simple Mode (Recommended for First-Time Users)

1. Leave "Custom Mode" unchecked
2. Enter a description: "A calm piano melody"
3. Run!

#### Custom Mode (Advanced)

1. Check "Custom Mode"
2. Fill in style, title, and detailed prompt
3. Fine-tune advanced parameters
```

### Music/Audio Generation API Patterns

#### Character Limits by Model

Many generation APIs have model-specific character limits. Store limits as class constants:

```python
class MusicGenerationNode(DataNode):
    # Prompt length limits by model
    PROMPT_LIMITS_CUSTOM = {
        "V3_5": 3000,
        "V4": 3000,
        "V4_5": 5000,
        "V5": 5000,
    }
    PROMPT_LIMIT_SIMPLE = 500

    # Style length limits by model
    STYLE_LIMITS = {
        "V3_5": 200,
        "V4": 200,
        "V4_5": 1000,
        "V5": 1000,
    }

    TITLE_LIMIT = 80

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate with model-specific limits."""
        exceptions = []
        model = self.get_parameter_value("model")
        custom_mode = self.get_parameter_value("custom_mode")

        if custom_mode:
            prompt = self.get_parameter_value("prompt") or ""
            prompt_limit = self.PROMPT_LIMITS_CUSTOM.get(model, 3000)
            if len(prompt) > prompt_limit:
                exceptions.append(ValueError(
                    f"{self.name}: Prompt exceeds {prompt_limit} character limit for {model} "
                    f"(current: {len(prompt)} characters)"
                ))

            style = self.get_parameter_value("style") or ""
            style_limit = self.STYLE_LIMITS.get(model, 200)
            if len(style) > style_limit:
                exceptions.append(ValueError(
                    f"{self.name}: Style exceeds {style_limit} character limit for {model} "
                    f"(current: {len(style)} characters)"
                ))

        return exceptions if exceptions else None
```

#### Model Selection with Detailed Tooltips

Use list of dict tooltip format for model comparison:

```python
model_param = Parameter(
    name="model",
    input_types=["str"],
    type="str",
    default_value="V5",
    tooltip=[
        {"type": "text", "text": "Model version for generation:"},
        {"type": "text", "text": "• V5: Superior quality, fastest (4 min max)"},
        {"type": "text", "text": "• V4_5PLUS: Richest sound, up to 8 min"},
        {"type": "text", "text": "• V4_5: Superior blending, up to 8 min"},
        {"type": "text", "text": "• V4: Best quality, refined structure (4 min)"},
        {"type": "text", "text": "• V3_5: Creative diversity (4 min)"},
    ],
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
)
model_param.add_trait(Options(choices=["V5", "V4_5PLUS", "V4_5", "V4", "V3_5"]))
```

#### Dual Track Output Pattern

APIs that generate multiple variations:

```python
# Output parameter for multiple tracks
music_urls_param = Parameter(
    name="music_urls",
    output_type="list[str]",
    type="list[str]",
    tooltip="Download URLs for generated tracks (2 variations)",
    allowed_modes={ParameterMode.OUTPUT},
    settable=False,
    ui_options={"is_full_width": True, "display_name": "Music URLs"},
)
self.add_parameter(music_urls_param)

def process(self) -> None:
    # ... generation logic ...
    urls = self._extract_music_urls(response_data)
    self.parameter_output_values["music_urls"] = urls

    # Build detailed result
    result_lines = [
        f"✓ Generated {len(urls)} track variation(s)",
        "",
        "Music URLs:",
    ]
    for i, url in enumerate(urls, 1):
        result_lines.append(f"{i}. {url}")

    self.parameter_output_values["result_details"] = "\n".join(result_lines)
```

#### Status Updates During Long Operations

Update status parameter in real-time during polling:

```python
def _poll_for_completion(self, task_id: str, api_key: str) -> dict[str, Any]:
    """Poll API with real-time status updates."""
    for attempt in range(self.MAX_POLLING_ATTEMPTS):
        time.sleep(self.POLLING_INTERVAL)

        # Update status parameter with progress
        status_msg = f"Generating... ({attempt + 1}/{self.MAX_POLLING_ATTEMPTS})"
        self.set_parameter_value("status", status_msg)

        response = requests.get(query_url, headers=headers, params={"ids": task_id})
        # ... check completion ...
```

**Best Practice**: Always provide progress feedback for operations longer than 10 seconds.

### Documentation Patterns for Node Libraries

#### Comprehensive README Structure

```markdown
# Library Name

Brief description and key features.

## Features

- Bullet list of main capabilities
- Include model options
- Highlight unique features

## Installation

### Option 1: Using uv (Recommended)

Steps for uv installation

### Option 2: Automatic Installation

Steps for pip installation

## Getting Started

### Simple Mode (Recommended for First-Time Users)

Minimal example with explanations

### Custom Mode (Advanced)

Advanced example showing all features

## Parameters

### Basic Parameters

Table with Name, Type, Description

### Advanced Parameters (Hidden by Default)

Table with Name, Type, Default, Description

### Output Parameters

Table with outputs

## Model Comparison

Table comparing models:
| Model | Max Duration | Quality | Speed | Character Limits |

## Character Limits

Clear tables showing limits by model/mode

## API Rate Limits

Document:

- Concurrency limits
- Generation time expectations
- File retention policies

## Example Workflows

3-5 complete examples covering common use cases

## Error Handling

Common errors and solutions

## Troubleshooting

### Common Errors and Solutions

#### Error: "Missing required variables: file_extension, file_name_base"

**Full Error:**

```

ERROR: Attempted to resolve macro path. Failed because missing required variables: file_extension, file_name_base
ERROR: Attempted to create download URL. Failed with file_path='{outputs}/{node_name?:\_}{file_name_base}{\_index?:03}.{file_extension}'

````

**Cause:** Not capturing the return value from `write_bytes()`. Using `dest.location` instead of `saved.location`.

**Incorrect Code:**

```python
dest = self._output_file.build_file()
dest.write_bytes(video_bytes)  # ❌ Return value not captured
artifact = VideoUrlArtifact(dest.location)  # Using dest, not saved file
````

**Solution:**

```python
dest = self._output_file.build_file()
saved = dest.write_bytes(video_bytes)  # ✅ Capture the saved file
artifact = VideoUrlArtifact(saved.location)  # Use saved file's resolved location
```

**Explanation:** Macro variables are populated when `write_bytes()` actually saves the file. The `saved` object returned by `write_bytes()` contains the fully resolved path.

______________________________________________________________________

#### Error: Type Conversion Issues with Image Parameters

**Symptom:** Image parameters don't handle different input types consistently, or errors occur when passing URLs, file paths, or artifacts between nodes.

**Problem:** Using generic `Parameter` with manual type configuration doesn't standardize type conversion logic:

```python
# ❌ Inconsistent type handling
Parameter(
    name="image",
    input_types=["ImageArtifact", "ImageUrlArtifact", "str"],
    type="ImageArtifact",
)
```

**Solution:** Use `ParameterImage` for standardized type conversion:

```python
# ✅ Standardized type handling
ParameterImage(
    name="image",
    tooltip="Input image",
    allow_output=False,
)
```

**Benefits:**

- Handles ImageArtifact, ImageUrlArtifact, and strings consistently
- Built-in support for URLs, file paths, and data URIs
- Graceful error handling for various input formats
- Reduces type conversion errors in complex workflows

______________________________________________________________________

FAQ-style troubleshooting guide

## API Reference

Link to official API docs

## Best Practices

Tips for optimal usage

## Support

Where to get help

## Version History

Link to CHANGELOG

````

#### Model Comparison Table

Always include a comparison table for services with multiple models:

```markdown
| Model | Max Duration | Quality  | Speed   | Character Limits          |
| ----- | ------------ | -------- | ------- | ------------------------- |
| V5    | 4 min        | Superior | Fastest | Prompt: 5000, Style: 1000 |
| V4_5  | 8 min        | High     | Fast    | Prompt: 5000, Style: 1000 |
| V4    | 4 min        | Best     | Medium  | Prompt: 3000, Style: 200  |
````

______________________________________________________________________

This guide represents the current best practices for Griptape node development, incorporating both foundational concepts and modern patterns demonstrated in production nodes. Use these patterns to create robust, user-friendly, and maintainable nodes that integrate seamlessly with the Griptape ecosystem.
