# The Editor

The editor is the visual workspace where you build, run, and inspect
Griptape Nodes workflows. It's a canvas: you drag nodes onto it, wire
them together, and run the result. Everything else in the window —
the header, the menu bar, the two sidebars — exists to support that
canvas: naming and running your workflow, finding nodes to add, and
inspecting what a node is doing once it runs.

This page is a tour of each region. If you're looking for a specific
task instead, the [Where to go next](#where-to-go-next) section links
to focused guides for nodes, groups, running workflows, media editors,
and library management.

<!-- screenshot (#5166): full editor window with the canvas, header, left sidebar, and right sidebar all visible and labeled -->

## The canvas

The canvas is where your workflow lives. Nodes are boxes with inputs
and outputs; connections between them are the lines you draw from one
node's output to another's input. Everything you build ends up here.

A few ways to move around:

- **Pan**: hold `Space` and drag (this works even while your
    cursor is over a node), or drag with the middle mouse button.
- **Zoom**: scroll or pinch, use the zoom slider in the top-left
    corner (if enabled in your settings), or hold `Z` and drag
    left/right for a Photoshop-style scrubby zoom.
- **Select**: drag on empty canvas to draw a selection box around
    several nodes, or Shift-click individual nodes to add or remove
    them from the current selection.

A **minimap** in the corner (toggle it in your editor settings if it's
hidden) shows the whole graph at a glance and lets you jump around by
clicking or dragging inside it. The controls cluster in the
bottom-left gives you zoom in/out/fit-view buttons and a **Toggle
Clean Mode** button that hides the decorative Griptape logo watermark
— useful when you're taking a screenshot of your own workflow.

Double-clicking empty canvas opens the **Add Node** menu at your
cursor. See [Working with Nodes](working_with_nodes.md) for everything
you can do with a node once it's placed, and
[Keyboard Shortcuts](keyboard_shortcuts.md) for the full list of
canvas shortcuts.

<!-- screenshot (#5166): canvas with a few connected nodes, the minimap visible in a corner, and the bottom-left controls cluster -->

## The header

The header runs across the top of the window and is split into three
clusters.

On the **left**, a sidebar-toggle button sits next to the
[menu bar](#the-menu-bar), followed by your workflow's name. The name
shows a trailing `*` whenever you have unsaved changes, and the file
name (`<workflow>.py`) appears underneath once the workflow has been
saved at least once. An unsaved, never-saved workflow shows just the
in-progress name with no file name line.

In the **center**, the run controls:

| Button                | What it does                                                   |
| --------------------- | -------------------------------------------------------------- |
| **Run Workflow**      | Runs the entire workflow from its start.                       |
| **Run To Selected**   | Runs up through the single selected node.                      |
| **Run From Selected** | Starts execution at the single selected node and runs forward. |
| **Cancel Run**        | Stops a workflow that's currently running.                     |

**Run To Selected** and **Run From Selected** are only enabled when
exactly one node is selected. See
[Running Workflows](running_workflows.md) for what "runnable" means
and how partial runs behave.

On the **right**, an engine picker (for switching which running engine
the editor talks to), an error history dropdown, and a **Publish
Workflow** button that opens the publish dialog.

<!-- screenshot (#5166): header close-up showing the workflow name with unsaved indicator, the run button cluster, and the right-side engine picker -->

## The menu bar

The menu bar sits in the header's left cluster, next to the sidebar
toggle. It has three menus.

### File

Create, open, save, and manage the current workflow file:

- **New** / **Open...** — start a new workflow or browse to an
    existing one.
- **Rename...** — rename the workflow (disabled for Griptape-provided
    templates; use Save As to make an editable copy instead).
- **Save** / **Save As...** / **Save As New Version** — Save As New
    Version writes a new versioned file (`my_workflow_v002.py`, and so
    on) instead of overwriting; it only appears once the workflow has
    been saved at least once.
- **AutoSave** — toggle automatic saving on or off, with a shortcut
    into its settings.
- **Refresh Libraries** — pick up library changes without restarting
    the engine.
- **Report Issue** — file a bug report.
- **Exit** — leave the editor.
- **Delete `<workflow name>`** — permanently delete the current
    workflow file.

### Manage

Jumps to the management surfaces for the resources a workflow depends
on:

- **Model Management** — models available to nodes that need them.
- **Library Management** — install, update, and configure node
    libraries. See [Libraries](../libraries.md) for the full guide.
- **Engine Management** — the engines the editor can connect to.
- **Project Management** — the projects available to switch between.
    See [Managing Projects in the GUI](../projects/gui_guide.md).

### Settings

Everything configurable, grouped under a **Settings** submenu (All
Settings, Agent Settings, Editor Settings, Theme Settings, Engine
Settings, File System, Libraries, Library Settings, MCP Servers, and
API Keys & Secrets), plus three actions below it: **Copy Path to
Settings** (copies the settings file's path to your clipboard),
**Show Settings Folder** (opens it in Finder/Explorer/your file
manager), and **Reset Settings to Default**.

<!-- screenshot (#5166): the menu bar with the File menu open, showing its items and shortcuts -->

## The left sidebar

The left sidebar is where you find nodes and libraries to add to your
workflow. A **Favorites** section sits above the tabs — star a node to
pin it here so you don't have to hunt for it every time — followed by
a search box that filters whichever tab is active.

Two tabs live below the search box:

- **Nodes** — every node available to you, organized by category, from
    both built-in and installed libraries.
- **Libraries** — the same nodes, organized by which library they come
    from instead of by category. This is a browsing view; to install,
    update, or remove a library itself, use **Manage → Library
    Management** (see [Libraries](../libraries.md)).

Drag a node from either tab onto the canvas to add it to your
workflow. Click the sidebar-toggle button in the header (or its
shortcut) to collapse the sidebar down to a narrow icon rail, or close
it entirely.

<!-- screenshot (#5166): left sidebar expanded, showing the Favorites section, search box, and the Nodes tab's category tree -->

## The right sidebar

The right sidebar holds a set of tabbed panels, titled **Sidebar
Panels**. A dropdown next to its close button (the icon with three
dots) lets you show or hide any panel — each entry shows an eye icon
so you can see which panels are currently visible. Hiding
a panel here doesn't discard anything; its tab returns the next time
you re-enable it.

The four panels:

- **Chat** — talk to an agent thread directly inside the editor, with
    a picker for model/provider and any MCP servers you've wired up.
- **Code** — the workflow's generated Python, in a syntax-highlighted
    editor you can search and run from.
- **Logs** — the execution log stream, filterable by severity (Debug,
    Info, Warning, Error, Critical) and searchable.
- **Properties** — the parameters of whichever node (or nodes) you
    currently have selected on the canvas. This is usually where
    you'll spend most of your time once a workflow is built, since
    it's how you configure each node's inputs.

If the sidebar is closed, a floating button near the top-right corner
of the canvas reopens it.

<!-- screenshot (#5166): right sidebar showing the panel tabs (Chat, Code, Logs, Properties) with the panel-visibility dropdown open -->

## Where to go next

- [Keyboard Shortcuts](keyboard_shortcuts.md) — every shortcut in the
    editor, grouped by what you're trying to do.
- [Working with Nodes](working_with_nodes.md) — adding, wiring,
    renaming, locking, and duplicating nodes.
- [Node Groups](node_groups.md) — grouping nodes and arranging your
    graph.
- [Running Workflows](running_workflows.md) — the difference between
    running a workflow, running to a node, and running from a node.
- [Media Viewers and Editors](media_editors.md) — the built-in
    viewers and editors for images, video, and other media parameters.
- [Managing Models and Libraries](managing_models_and_libraries.md) —
    the Model Management and Library Management surfaces in more
    depth.
