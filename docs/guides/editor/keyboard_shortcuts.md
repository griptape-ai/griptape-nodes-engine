# Keyboard Shortcuts

A reference for every shortcut in the editor, grouped by what you're
trying to do. macOS and Windows/Linux shortcuts are listed in separate
columns where the modifier keys differ.

!!! note "Typing is never interrupted"

    Most single-key canvas shortcuts (like `R` or `N`) are ignored
    while you're typing in a text field, a node's parameter, or the
    Code panel, so ordinary letters always go where you're typing
    instead of triggering a shortcut.

For a tour of the regions these shortcuts act on, see
[The Editor](index.md).

## Adding & editing nodes

| Action                                                     | macOS               | Windows / Linux     |
| ---------------------------------------------------------- | ------------------- | ------------------- |
| Add a node at the cursor (canvas only)                     | Tab                 | Tab                 |
| Add a node at the cursor, or select all if not over canvas | Shift+A             | Shift+A             |
| Open the Add Node menu at the cursor                       | Double-click canvas | Double-click canvas |
| Create a Note node at the cursor                           | N                   | N                   |
| Rename the selected node(s)                                | R                   | R                   |
| Duplicate the selected node(s)                             | Cmd+D               | Ctrl+D              |
| Lock / unlock the selected node(s)                         | L                   | L                   |
| Delete the selected node(s) or connection                  | Delete / Backspace  | Delete / Backspace  |

## Selection

| Action                                                | macOS                | Windows / Linux      |
| ----------------------------------------------------- | -------------------- | -------------------- |
| Select all nodes                                      | Cmd+A                | Ctrl+A               |
| Clear the current selection                           | Escape               | Escape               |
| Add or remove a node from the selection               | Shift-click a node   | Shift-click a node   |
| Draw a selection box around several nodes             | Drag on empty canvas | Drag on empty canvas |
| Move the selection to the nearest node in a direction | Arrow keys           | Arrow keys           |

## Clipboard

| Action                                                 | macOS | Windows / Linux |
| ------------------------------------------------------ | ----- | --------------- |
| Copy the selected node(s)                              | Cmd+C | Ctrl+C          |
| Paste nodes, or an image/media file from the clipboard | Cmd+V | Ctrl+V          |

## Navigation & view

| Action                                                             | macOS               | Windows / Linux     |
| ------------------------------------------------------------------ | ------------------- | ------------------- |
| Frame the selected node(s), or the whole graph if nothing selected | F                   | F                   |
| Fit a selected group to its nodes (otherwise frames the selection) | Shift+F             | Shift+F             |
| Pan the canvas, even while over a node                             | Hold Space + drag   | Hold Space + drag   |
| Pan the canvas                                                     | Middle-click + drag | Middle-click + drag |
| Scrubby zoom — drag right to zoom in, left to zoom out             | Hold Z + drag       | Hold Z + drag       |
| Zoom in                                                            | Cmd+=               | Alt+=               |
| Zoom out                                                           | Cmd+-               | Alt+-               |
| Zoom in / out                                                      | Scroll or pinch     | Scroll or pinch     |

## Groups & layout

| Action                                           | macOS       | Windows / Linux |
| ------------------------------------------------ | ----------- | --------------- |
| Create a group from the selection (default type) | Cmd+G       | Ctrl+G          |
| Create a group, always choosing the type         | Shift+Cmd+G | Shift+Ctrl+G    |
| Auto-arrange the graph                           | Shift+G     | Shift+G         |

See [Node Groups](node_groups.md) for what the group types mean and
how auto-arrange lays out your graph.

## Connections

| Action                                                         | macOS    | Windows / Linux |
| -------------------------------------------------------------- | -------- | --------------- |
| Toggle display of parameters that already have a connection    | Shift+H  | Shift+H         |
| Insert a dot (reroute) node on the hovered/selected connection | I        | I               |
| Show a connection's action buttons while hovering over it      | Hold Cmd | Hold Ctrl       |

## Workflow / file

| Action                            | macOS           | Windows / Linux  |
| --------------------------------- | --------------- | ---------------- |
| Save                              | Cmd+S           | Ctrl+S           |
| Save As                           | Shift+Cmd+S     | Shift+Ctrl+S     |
| Save As New Version               | Option+Shift+S  | Alt+Shift+S      |
| Open Workflow                     | Cmd+O           | Ctrl+O           |
| New Workflow                      | Shift+Cmd+O     | Shift+Ctrl+O     |
| Run to the selected node          | Cmd+Enter       | Ctrl+Enter       |
| Run the whole workflow            | Shift+Cmd+Enter | Shift+Ctrl+Enter |
| Download the workflow as an image | Cmd+I           | Ctrl+I           |
| Toggle the right sidebar          | Cmd+B           | Ctrl+B           |
| Toggle the left sidebar           | Shift+Cmd+B     | Shift+Ctrl+B     |
| Show this shortcuts reference     | Shift+?         | Shift+?          |
