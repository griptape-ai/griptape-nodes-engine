# Parameters

Parameters define the inputs, outputs, and properties of a node. This page covers every `Parameter` attribute, the traits system, the parameter helper classes, containers, and advanced patterns for dynamic parameter surfaces.

For the mapping from parameter types to editor widgets, supported `ui_options` keys, and traits, see the [Parameter UI Reference](parameter_ui_reference.md).

## Parameter Attributes

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

## Traits

Add functionality via `add_trait()`:

- **Options**: `Options(choices=list[str] | list[tuple[str, Any]], show_search: bool = True, search_filter: str = "")`
- **Slider**: `Slider(min_val: float, max_val: float)`
- **Button**: `Button(label: str = "", variant=..., size=..., button_link=... | on_click=..., get_button_state=...)`
- **ColorPicker**: `ColorPicker(format="hex")`
- **FileSystemPicker**: `FileSystemPicker(...)` (file/directory selection UI)

For the full list of traits, the widgets they render, and the `ui_options` keys they manage, see the [Parameter UI Reference](parameter_ui_reference.md).

## Parameter helper constructs (`ParameterString`, `ParameterInt`, ...)

Griptape Nodes includes a set of convenience Parameter subclasses under `griptape_nodes.exe_types.param_types.*`.
They exist to make common parameter patterns **simple, consistent, and runtime-mutable** (many expose UI options as Python properties).

### Quick reference table

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

### Shared behavior across helpers

- All helpers forward the standard `Parameter` constructor knobs (`allowed_modes` or `allow_input/allow_property/allow_output`, `hide/hide_label/hide_property`, `settable`, `serializable`, etc.).
- Many helpers default `accept_any=True`. When enabled, the helper typically sets `input_types=["any"]` and prepends a converter (e.g. `ParameterString` converts any input to `str`). Turn this off if you want strict typing.
- If you provide both an explicit convenience parameter (e.g. `hide=True`) and the same key in `ui_options` (e.g. `ui_options={"hide": False}`), **`ui_options` wins** and Griptape Nodes will warn about the conflict.

### Detailed helper notes

#### `ParameterString`

- Enforces `type="str"` and `output_type="str"`.
- `accept_any=True` converts `None` → `""` and otherwise uses `str(value)`.
- UI convenience: `markdown`, `multiline`, `placeholder_text`, `is_full_width` (all are runtime-settable properties).

#### `ParameterBool`

- Enforces `type="bool"` and `output_type="bool"`.
- `accept_any=True` converts common string representations (e.g. `"true"`, `"yes"`, `"on"`, `"1"`) to `True` and (`"false"`, `"no"`, `"off"`, `"0"`) to `False`.
- UI convenience: `on_label`, `off_label` (runtime-settable properties).

#### `ParameterInt` / `ParameterFloat` (via `ParameterNumber`)

- Enforces numeric `type` / `output_type` and can prepend a converter when `accept_any=True`.
- `step`: stored in `ui_options["step"]` and validated (value must be a multiple of the current step).
- `slider`, `min_val`, `max_val`, `validate_min_max`: adds one of these constraint traits based on priority:
    - `Slider(min_val, max_val)` if `slider=True`
    - `MinMax(min_val, max_val)` if `validate_min_max=True`
    - `Clamp(min_val, max_val)` if `min_val` and `max_val` are provided

#### `ParameterJson`

- Enforces `type="json"` and `output_type="json"`.
- `accept_any=True` attempts to repair/parse JSON strings using `json_repair.repair_json()` (and will also attempt to stringify non-string inputs).
- UI convenience: optional editor button (`button`, `button_label`, `button_icon`).

#### `ParameterDict`

- Enforces `type="dict"` and `output_type="dict"`.
- `accept_any=True` uses `to_dict(...)` to coerce common inputs into a dict.

#### `ParameterRange`

- Enforces `type="list"` and `output_type="list"`.
- `accept_any=True` coerces `None` → `[]`, list → list, and any other value → `[value]`.
- UI convenience: `range_slider` (a nested `ui_options["range_slider"]` object) with `min_val/max_val/step` and label visibility options.
- The range slider UI is only applicable when the value is a list of exactly two numeric values.

#### `ParameterImage` (Recommended for Image Parameters)

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

#### `ParameterAudio` / `ParameterVideo` / `Parameter3D`

- Enforce their corresponding `*UrlArtifact` `type` / `output_type` (e.g., `AudioUrlArtifact`, `VideoUrlArtifact`, `ThreeDUrlArtifact`).
- These helpers primarily provide UI options (file browser / capture / editing / expanders). If you need coercion from e.g. `str` → artifact, supply `converters` and/or handle it in your node's `before_value_set()` / `process()` logic.
- Follow the same patterns as `ParameterImage` for these media types.

#### `ParameterButton`

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

## Containers

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

## ParameterList Pattern

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

## Common Parameter Patterns

### Search Input with Placeholder

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

### Full-Width List Output

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

### Multiline Text Input

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

### File Upload with Browser

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
