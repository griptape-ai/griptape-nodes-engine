# Custom Widgets

Nodes can use custom JavaScript widget components to provide rich, interactive UI beyond the standard parameter controls. Widgets are standalone `.js` files that render into a container element and communicate value changes back to the framework via a callback.

## Widget Architecture

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

## Registering a Widget

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

## Attaching a Widget to a Parameter

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

## Widget JS Function Signature

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

## Critical Patterns and Pitfalls

### Emit Changes Sparingly — Not on Every Keystroke

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

### Prevent Node Drag Interference

Node canvases handle drag events for panning and node movement. Interactive elements inside your widget must stop event propagation and use the `nodrag` / `nowheel` CSS classes:

```javascript
// On the outermost wrapper
const wrapper = document.createElement("div");
wrapper.className = "my-widget nodrag nowheel";

// On interactive child elements (textareas, sliders, etc.)
textarea.addEventListener("pointerdown", (e) => e.stopPropagation());
textarea.addEventListener("mousedown", (e) => e.stopPropagation());
```

### Prevent Keyboard Shortcut Interference

The node editor binds keyboard shortcuts at the canvas level — for example, pressing `Delete` deletes the selected node. When a text input inside your widget has focus, these shortcuts still fire because keyboard events bubble up. Stop propagation on `keydown` to isolate your text inputs:

```javascript
textarea.addEventListener("keydown", (e) => e.stopPropagation());
```

This prevents the Delete key from deleting the node while the user is editing text, and stops other canvas-level shortcuts (copy, paste, undo at the node level) from interfering with normal text editing.

### Override `user-select: none` for Text Inputs

Widget wrappers typically set `user-select: none` to prevent accidental text selection during drag operations. This cascades into child elements and blocks textarea editing. Override it explicitly:

```css
textarea {
  user-select: text;
  -webkit-user-select: text;
}
```

### Clone Values Before Emitting

Always pass a fresh copy to `onChange` — not a reference to your internal state. Otherwise the framework and your widget share the same object, leading to subtle bugs:

```javascript
onChange(localData.map((item) => ({ ...item })));
```

### Clean Up Document-Level Listeners

If you attach listeners to `document` (e.g., for drag-and-drop or click-outside-to-close), remove them in the cleanup function:

```javascript
document.addEventListener("pointerdown", onDocumentClick, true);

return () => {
  document.removeEventListener("pointerdown", onDocumentClick, true);
};
```

### Assign Stable IDs to List Items

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

### Handle the `disabled` Attribute Correctly in DOM Helpers

If you write a DOM helper function that creates elements from an attributes object, be careful with the `disabled` attribute. Using `setAttribute("disabled", false)` does **not** remove the disabled state — the presence of the attribute in any form disables the element. Use the property instead:

```javascript
if (key === "disabled") {
  element.disabled = !!val;
}
```

### Show Drop Indicators at End of List

When implementing drag-and-drop reordering, the drop target indicator (e.g., a blue border) must also appear when dragging past the last item in the list. A common approach: when the drag position is below all items, show a `border-bottom` on the last item instead of a `border-top` on a nonexistent next item:

```javascript
if (dragOverIndex === items.length && item.index === lastIndex) {
  item.el.style.borderBottom = "2px solid #4a9eff";
} else if (item.index === dragOverIndex) {
  item.el.style.borderTop = "2px solid #4a9eff";
}
```

### Enforce Aggregate Constraints (Min/Max Totals)

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

## Example: List-Based Editor Widget

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

## Widget Testbed

The **widget-testbed** is a standalone React + Vite application for testing and developing custom widget components outside the full Griptape Nodes environment. It provides a lightweight, hot-reloading development environment where you can iterate quickly on widget UI and behavior.

### Purpose

Custom widgets for Griptape Nodes are imperative JavaScript functions that manage their own DOM and state. The widget-testbed allows you to:

- **Rapid prototyping**: Test widget behavior with instant hot-reload during development
- **Isolated testing**: Work on widget UI/UX without launching the full Griptape Nodes application
- **State management verification**: Test complex state transitions and user interactions
- **Cross-widget development**: Easily switch between testing different widgets
- **Debug UI issues**: Inspect the widget's rendered output and state in a clean environment

### When to Use

Use the widget-testbed when:

- Creating a new custom widget component from scratch
- Debugging widget behavior issues (focus loss, drag-and-drop, event handling)
- Testing widget state management and `onChange` callback patterns
- Verifying widget appearance and layout without node editor interference
- Developing widgets that manage complex internal state (lists, editors, multi-step forms)

### File Structure

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

### Key Components

#### WidgetHost.jsx

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

#### App.jsx

The main test harness that provides:

- **Widget mounting**: Imports and renders the widget via `WidgetHost`
- **State controls**: Checkbox to toggle disabled state
- **Debug panel**: JSON view of current widget state (toggle with checkbox)
- **Reset functionality**: Button to reset widget to initial state
- **Visual layout**: Clean, dark-themed UI matching Griptape Nodes aesthetic

### Testing a Widget

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

### Development Workflow

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

### WidgetHost Pattern Details

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

### Best Practices

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

### Example: MultiShotEditor Testbed

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
