# Parameter UI Reference

This page maps what you declare on a `Parameter` in Python to what the editor renders for it. Three things determine a parameter's UI:

1. **The parameter's `type`** selects the widget: `str` gets a text field, `bool` gets a toggle, `ImageArtifact` gets the image viewer, and so on.
1. **`ui_options`** tweaks that widget: hide it, stretch it full-width, make a text field multiline, add a webcam capture button.
1. **Traits** bundle UI and behavior together: `Slider` renders a slider *and* validates the range, `Options` renders a dropdown *and* constrains the value to its choices. Under the hood, a trait writes its own keys into `ui_options` — traits are the supported way to get those keys right.

Prefer the [parameter helper classes](parameters.md#parameter-helper-constructs-parameterstring-parameterint) (`ParameterString`, `ParameterImage`, ...) and traits when one exists for what you want; reach for raw `ui_options` keys only for the presentation tweaks listed here.

!!! warning "Undocumented keys are internal"

    The editor reads more `ui_options` keys than are listed on this page. Anything not listed here (or emitted by a trait) is editor-internal and may change or disappear without notice.

## How the widget is chosen

For each parameter, the editor picks a widget in this order:

1. If `ui_options` carries `widget` and `library` (set by the [`Widget` trait](#traits)), the editor loads that custom widget from the library.
1. Otherwise, the parameter's `type` selects a built-in widget from the table below. A `list[...]` type (any element type) selects the list widget.
1. A type with no mapping gets no inline widget at all — the parameter still shows its label and connection handles, but there's nothing to edit in place.

## Type-to-widget mapping

| `type`                                                                                                                                                                                         | Widget                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `str`                                                                                                                                                                                          | Single-line text field (multiline, Markdown, and picker variants via `ui_options` and traits below)                          |
| `int`, `float`                                                                                                                                                                                 | Number input (slider via the `Slider` trait; step size via `step`)                                                           |
| `bool`                                                                                                                                                                                         | Toggle switch                                                                                                                |
| `json`, `JsonArtifact`                                                                                                                                                                         | JSON viewer/editor                                                                                                           |
| `python`, `yaml`                                                                                                                                                                               | Syntax-highlighted code editor                                                                                               |
| `html`, `xml`                                                                                                                                                                                  | Code editor in HTML/XML mode                                                                                                 |
| `dict`                                                                                                                                                                                         | Key-value editor; also hosts the image/video comparison sliders (see [dict options](#dict))                                  |
| `list`, `list[...]`                                                                                                                                                                            | List editor rendering one item widget per element                                                                            |
| `button`                                                                                                                                                                                       | Clickable button (configure with the `Button` trait)                                                                         |
| `Status`                                                                                                                                                                                       | Status/message block                                                                                                         |
| `UrlArtifact`                                                                                                                                                                                  | URL display                                                                                                                  |
| `ImageArtifact` / `ImageUrlArtifact`, `VideoArtifact` / `VideoUrlArtifact`, `AudioArtifact` / `AudioUrlArtifact`, `ThreeDArtifact` / `ThreeDUrlArtifact`, `SplatArtifact` / `SplatUrlArtifact` | The media viewers and editors — see [Media Viewers and Editors](../../guides/editor/media_editors.md) for what each one does |
| anything else                                                                                                                                                                                  | No inline widget; label and connection handles only                                                                          |

`GLTFArtifact` / `GLTFUrlArtifact` still render (as the 3D viewer) for backward compatibility; use the `ThreeD` types in new nodes.

## Common `ui_options` (any type)

| Key                         | Effect                                                                                                |
| --------------------------- | ----------------------------------------------------------------------------------------------------- |
| `hide`                      | Hide the parameter entirely (label, widget, and handles).                                             |
| `hide_label`                | Hide the name label, keep the widget.                                                                 |
| `hide_property`             | Hide the inline widget, keep the label and connection handles.                                        |
| `display_name`              | Label text shown in the UI, when it should differ from the parameter's name.                          |
| `is_full_width`             | Stretch the widget across the node's full width.                                                      |
| `parameter_render_location` | Where the parameter renders relative to its siblings: `"top"`, `"in-order"` (default), or `"bottom"`. |

## Per-type `ui_options`

### `str`

| Key                | Effect                                                                   |
| ------------------ | ------------------------------------------------------------------------ |
| `multiline`        | Multi-line text area instead of a single-line field.                     |
| `markdown`         | Render/edit the text as Markdown.                                        |
| `placeholder_text` | Placeholder shown while the field is empty (also works on code editors). |

### `int` / `float`

| Key            | Effect                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------- |
| `step`         | Increment used by the input's stepper; the engine also validates values against it.                      |
| `progress_bar` | Render the value as a progress bar instead of an editable input (for values a node reports, like 0–100). |

For a bounded slider, use the `Slider` trait rather than writing `ui_options["slider"]` by hand — the trait also validates the range.

### Image types

| Key                      | Effect                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------ |
| `clickable_file_browser` | Clicking the empty parameter opens a file picker; dropped/picked files are uploaded. |
| `expander`               | Let the viewer expand/collapse.                                                      |
| `crop` / `crop_image`    | Show the crop button that opens the crop editor.                                     |
| `edit_mask`              | Show the mask button that opens the Paint Mask editor.                               |
| `edit_excalidraw`        | Show the edit button that opens Image Bash.                                          |
| `webcam_capture_image`   | Replace the thumbnail with a live webcam preview and capture button.                 |
| `aspect_ratio`           | Fix the display area to an aspect ratio (e.g. `"16:9"`).                             |
| `object_fit`             | How the image fills its frame (CSS object-fit values like `"contain"` / `"cover"`).  |
| `hide_details`           | Hide the name/dimensions/file-size line under the thumbnail.                         |
| `pulse_on_run`           | Pulse the viewer while the node is executing (also works on audio).                  |

### Audio types

| Key                        | Effect                                                  |
| -------------------------- | ------------------------------------------------------- |
| `clickable_file_browser`   | Click-to-browse upload, same as images.                 |
| `microphone_capture_audio` | Show a record button that captures from the microphone. |
| `pulse_on_run`             | Pulse the player while the node is executing.           |

### `dict`

| Key             | Effect                                                                                                                                                 |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `compare`       | Render the dict (`{"input_image_1": ..., "input_image_2": ...}`) as the image comparison slider. Pair with `CompareImagesTrait` to validate the shape. |
| `video_compare` | Render the dict as the side-by-side video comparison player.                                                                                           |

### `json`

| Key     | Effect                                             |
| ------- | -------------------------------------------------- |
| `modal` | Open the JSON editor in a modal instead of inline. |

### `list`

| Key         | Effect                                          |
| ----------- | ----------------------------------------------- |
| `collapsed` | Start the list collapsed.                       |
| `columns`   | Lay items out in a grid with this many columns. |

`ParameterList` sets the common list options for you — see [ParameterList Pattern](parameters.md#parameterlist-pattern).

## Traits

Traits live in `griptape_nodes.traits` and are attached with `add_trait()` or `traits={...}` on the parameter. Each row lists what the trait renders and, where relevant, the `ui_options` keys it manages — set the trait rather than the keys.

| Trait                | Typical types             | What it does                                                                                          | `ui_options` it writes                                   |
| -------------------- | ------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `Options`            | `str`, any                | Dropdown constrained to fixed choices, with optional search.                                          | `simple_dropdown`, `show_search`, `search_filter`        |
| `MultiOptions`       | `list`                    | Multi-select dropdown.                                                                                | `multi_options`                                          |
| `Slider`             | `int`, `float`            | Slider between `min_val` and `max_val`; out-of-range values fail validation.                          | `slider`                                                 |
| `Clamp`              | `int`, `float`, sequences | Clamps the value into range on assignment. No UI of its own.                                          | —                                                        |
| `Button`             | `button`                  | Configures a button's label, variant, size, and click behavior.                                       | `button_label`, `variant`, `size`, `state`, `full_width` |
| `ColorPicker`        | `str`                     | Color swatch that opens a color picker; validates the format (`"hex"`, etc.).                         | `color_picker`                                           |
| `FileSystemPicker`   | `str`                     | Browse button that opens a file/directory picker with filtering options.                              | `fileSystemPicker`                                       |
| `NumbersSelector`    | `dict`                    | Min/max/step numeric range selector.                                                                  | `numbers_selector`                                       |
| `CompareImagesTrait` | `dict`                    | Validates the two-image dict shape used by the comparison slider (pair with `ui_options["compare"]`). | —                                                        |
| `Widget`             | any                       | Replaces the built-in widget with a custom widget shipped by a library.                               | `widget`, `library`                                      |

See the [Traits section of the Parameters reference](parameters.md#traits) for constructor signatures and examples.
