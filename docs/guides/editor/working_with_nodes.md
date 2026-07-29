# Working with Nodes

This page covers the everyday mechanics of building a workflow on the canvas:
adding nodes, selecting and moving them, renaming, duplicating, locking,
deleting, wiring connections between them, and editing their parameters. For
running a workflow once it's built, see
[Running Workflows](running_workflows.md). For grouping nodes together, see
[Node Groups](node_groups.md).

## Adding nodes

There are three ways to open the **Add Node** menu:

- Press `Tab` while the cursor is over the canvas.
- Press `Shift+A` while the cursor is over the canvas. (`Shift+A` with the
    cursor off the canvas selects all nodes instead — see
    [Selecting and moving](#selecting-and-moving).)
- Right-click empty canvas and choose **Add Node**.

The menu opens at your cursor and drops the new node there once you pick one.
It's a searchable, categorized list — type to filter by name, description, or
tag, or browse the category tree. A **Favorites** section (star a node to add
it) and a **Recent** section (your last few added node types) sit above the
category list when you have either.

<!-- screenshot (#5166): the Add Node menu open at the cursor, showing search results and category tree -->

You can also drag a node type from the **Nodes** tab in the left sidebar
straight onto the canvas.

## Selecting and moving

Click a node to select it. `Cmd/Ctrl+A` selects every node in the flow;
`Escape` clears the selection. Drag a box across empty canvas to select
everything inside it.

Shift-click (or Cmd/Ctrl-click) additional nodes to build a multi-selection.
With more than one node selected, dragging any of them moves the whole
selection together, and a floating toolbar appears above the selection with
actions that apply to all of them at once — Copy, Duplicate, Lock, Reset,
Delete, and Create Group, depending on what's configured in **Settings →
Editor → Button Customization**.

<!-- screenshot (#5166): two or more selected nodes with the floating multi-node toolbar visible above them -->

The arrow keys move the selection to the nearest node in that direction,
which is a fast way to step through a chain of connected nodes without
touching the mouse. `F` frames the current selection (or the whole graph, if
nothing is selected); `Shift+G` auto-arranges the graph into a tidy layout.

## Renaming

Every node's display name doubles as its unique identifier in the workflow,
so renaming a node also updates every reference to it.

To rename a single node, select it and press `R`, or click the small pencil
icon that appears next to its name on hover, or choose **Rename** from its
right-click menu. Type the new name and press `Enter` (or click away) to
commit it, or `Escape` to cancel.

To rename several nodes at once, select them and press `R`, or right-click
one of them and choose **Rename**. This opens the **Rename nodes** dialog,
which takes a single base name and applies it across the whole selection:
the first node gets the base name exactly, and the rest get `Base_1`,
`Base_2`, and so on.

<!-- screenshot (#5166): the Rename nodes dialog with a base name typed in and the preview text showing Base, Base_1, Base_2 -->

## Duplicating, copying, and pasting

- **Duplicate** (`Cmd/Ctrl+D`, or the Duplicate action on the node or the
    multi-node toolbar) creates an independent copy of the selected node(s)
    right next to the originals, with no connections to anything.
- **Copy** (`Cmd/Ctrl+C`) serializes the selected node(s) — including their
    parameter values — to the system clipboard.
- **Paste** (`Cmd/Ctrl+V`) creates new nodes from whatever you last copied,
    dropped near your cursor.

Copy/paste is also how you move nodes between two open workflow tabs: copy in
one, switch tabs, paste in the other.

## Locking

Locking a node protects it from accidental changes: a locked node can't be
run, deleted, have its parameters edited, or have parameters added to it
until you unlock it again.

Toggle a node's lock state with `L` (works on a multi-selection too), the
lock icon in its header, or **Lock**/**Unlock** in its right-click menu. Note
nodes can't be locked — the lock option doesn't apply to them.

## Deleting

Select one or more nodes and press `Delete` (or `Backspace` on some
keyboards), or use **Delete Node** from the right-click menu or the toolbar.
Deleting a node removes any connections it had; deleting a
[dot node](#dot-and-reroute-nodes) that sits in the middle of a connection
instead reconnects the two ends directly, so the rest of the wire survives.

Deleting a group only removes the group wrapper — its child nodes stay on
the canvas, ungrouped. See [Node Groups](node_groups.md#ungrouping) for
more.

## Connecting parameters

Every parameter that can act as an input or output gets a small handle on
the left (input) or right (output) edge of its row. Drag from one handle to
another to create a connection; drag from empty canvas off a handle to open
the Add Node menu pre-wired to that handle, which is a quick way to add the
*next* node in a chain.

The editor only lets you complete a connection between compatible types — as
you drag, incompatible handles elsewhere on the canvas dim out so you can see
where the connection can land. Most parameters accept more than
one type (an image input that also accepts a URL string, for example), and
the specific accepted types are baked into each parameter by its node.

To remove a connection, click its wire to select it and press `Delete`, or
hover the wire and use the connection button that appears on it (if **Show
Connection Buttons on Hover** is enabled in **Settings → Editor → Node
Settings**).

## Dot and reroute nodes

A **dot node** is a zero-logic pass-through you can drop in the middle of an
existing connection to bend its path around other nodes, or just to tidy up
a busy canvas. With a connection hovered or selected, press `I` to insert a
dot node right at that spot — the original connection is split into two,
routed through the new dot node, with no change to the data that flows
through it.

<!-- screenshot (#5166): a connection with a dot node inserted partway along its path -->

Deleting a dot node reconnects its two neighbors directly, so removing one
never breaks the flow.

## Notes

A **note** is a free-floating, non-executing block of text you drop on the
canvas to leave yourself or a collaborator context — what a section of the
graph is for, a TODO, a warning about an upstream dependency. Press `N` with
the cursor over the canvas to drop one at your cursor.

Notes can't be locked, and don't participate in execution or in the
multi-node toolbar's Run/Reset actions.

## The Properties panel

Selecting a node opens its parameters in the **Properties** panel (right
sidebar) alongside the parameter rows shown inline on the node itself — the
same values, just in a dedicated, always-visible place that's easier to work
in when a node has a lot of parameters or you've collapsed the node itself.
Open the right sidebar that hosts it with `Cmd/Ctrl+B`, from the
right-click menu's **Property Panel** entry, or from a node's header button if
you've added it there.

<!-- screenshot (#5166): the Properties panel showing a selected node's parameters -->

Editing a value in the Properties panel and editing the same parameter's
inline row on the node update the same underlying value.

### Adding a custom parameter

Some nodes let you add parameters of your own on top of the ones the node
ships with. If a node supports it, you'll see an **Add parameter** row at the
bottom of its parameter list (on the node itself and in the Properties
panel), or **Add parameter** in its right-click menu.

Opening it shows the **Add New Parameter** dialog, where you set:

| Field              | What it controls                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Name**           | The parameter's internal name. Must be unique on the node.                                                 |
| **Display Name**   | The label shown in the UI, if you want it to read differently from the internal name.                      |
| **Type**           | One of `Any`, `str`, `bool`, `int`, `float`, `dict`, `json`, or the media types `image`, `audio`, `video`. |
| **Tooltip**        | Help text shown on hover.                                                                                  |
| **Input / Output** | Whether the parameter can be wired as an incoming connection, an outgoing connection, or both.             |
| **Default Value**  | The value the parameter starts with before you set or connect anything.                                    |

Depending on the type you pick, more options appear — a multiline/Markdown
toggle for text, a slider with min/max/step for numbers, or a fixed set of
choices (with optional icons, subtitles, and a search box) for a dropdown.

<!-- screenshot (#5166): the Add New Parameter dialog with the type dropdown open -->

### Configuring an existing custom parameter

Once a custom parameter exists, right-click it (or its equivalent row in the
Properties panel) and choose **Configure parameter** to reopen the same
dialog pre-filled with its current settings. This option only appears for
parameters you added yourself — parameters that ship with the node's
definition aren't editable this way.

### Hiding connected parameters

Turn on **Hide Connected Parameters** (`Shift+H`, or **Settings → Editor →
Node Settings**) to collapse any parameter row whose input already has an
incoming connection. It's a decluttering tool for busy workflows: once a
value is coming from somewhere else on the canvas, you usually don't need to
see its editable field taking up space too. Toggling it back off restores
every row immediately.

!!! tip "Keep specific parameters visible anyway"

    **Ignore Hide Connected (Display Nodes)** and **Ignore Hide Connected
    (Save Nodes)**, next to the main toggle in Node Settings, exempt any
    parameter whose name contains "display" or "save" — useful for nodes
    where you want to keep an eye on an output path or preview even while
    its input is wired up.
