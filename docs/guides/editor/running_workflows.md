# Running Workflows

This page covers everything that happens once your workflow is built and
you want to actually run it: starting a run, stopping one, reading what's
happening on the canvas while it runs, checking the logs and error history
afterward, and saving your work.

## Running the whole workflow

The **Run Workflow** button, in the center of the header, runs every node
in the current workflow from the start. It's disabled while a workflow is
already running, and while no workflow is open.

<!-- screenshot (#5166): the header's run button group (Run Workflow, Run To Selected, Run From Selected, Cancel Run) -->

| Shortcut                        | Action       |
| ------------------------------- | ------------ |
| `Shift` + `Cmd/Ctrl` + `Return` | Run Workflow |

## Running a single node

Two more buttons next to Run Workflow let you run less than the whole
graph. The buttons require exactly one node to be selected. Run From
Selected is disabled while the workflow is already running; Run To
Selected is not — see [Running a node during a
run](#running-a-node-during-a-run) below.

- **Run To Selected** resolves the selected node — and everything upstream
    of it that isn't already resolved — without touching anything
    downstream. This is the usual way to iterate: change a parameter, run
    to the node you're looking at, check the result.
- **Run From Selected** starts execution at the selected node and runs
    forward from there through the rest of the flow, the same way Run
    Workflow would, just starting partway through.

| Shortcut              | Action          |
| --------------------- | --------------- |
| `Cmd/Ctrl` + `Return` | Run To Selected |

Run From Selected has no keyboard shortcut; use the button. Unlike the
buttons, the `Cmd/Ctrl` + `Return` shortcut also works with several nodes
selected — it runs to each of them.

Both single-node actions are also available from the node's right-click
context menu, and from the multi-node toolbar when applicable.

## Running a node during a run

You don't have to wait for a run to finish to try a node. **Run To
Selected** stays available while the workflow is running, and the node
joins the run that's already in progress instead of starting a new one.
Anything upstream of it that isn't resolved yet is pulled into the same
run and resolved first, exactly as it would be from a standing start.

When the node actually begins depends on how much of the run is already
in flight:

- In **Parallel** execution mode it starts as soon as the run has a free
    slot, which may be immediately. The number of nodes that can run at
    once is the `max_nodes_in_parallel` setting (5 by default).
- In **Sequential** execution mode one node runs at a time, so the node
    you played starts once the node currently running finishes.

Both modes are described in
[Configuration](../configuration.md).

Two cases are worth knowing about:

- **If the run is paused**, the node is added to it but does not start.
    It runs when you step or continue the run.
- **If the node is already part of the run** — already finished, running,
    or queued behind its dependencies — playing it is refused rather than
    run twice, and the editor reports that the node is already executing.
    Cancel the run if you want to start it over.

**Run Workflow** and **Run From Selected** still require an idle
workflow; they stay disabled until the current run ends.

## Stopping a run

While a workflow is running, the same button area shows **Cancel Run** in
place of the run buttons. Click it to cancel the in-flight execution. The
button shows **Cancelling…** while the cancellation request is in flight.

Canceling a run is also how you get out of the **Cannot Save While Flow is
Running** dialog (see [Saving](#saving) below) — that dialog offers a
**Cancel Flow** button that does the same thing.

## Reading execution state on the canvas

Every node shows a status pill in its header while it's involved in the
current run:

| Pill                           | Meaning                                                |
| ------------------------------ | ------------------------------------------------------ |
| **Running** (orange, spinning) | The node is actively resolving right now.              |
| **Resolved** (green)           | The node finished successfully.                        |
| **Error** (red)                | The node failed. Hover the pill for the error message. |
| **Unresolved** (blue)          | The node hasn't run yet in this pass.                  |

A small asterisk next to a node's name also marks it as unresolved,
independent of whether it's part of the current run. After a run ends, a
node that errored keeps its red **Error** pill so you can still find it and
inspect the message without having to remember which node failed.

## The Logs panel

The **Execution Log** panel (right sidebar) collects everything the engine
logs while your workflow runs — engine messages as well as anything a node
itself logs, tagged with the node's name when available.

<!-- screenshot (#5166): the Execution Log panel showing a mix of INFO/WARNING/ERROR entries with node badges -->

What you can do from the panel:

- **Log Level Visibility** — a dropdown that sets the engine's logging
    verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). This
    changes what gets logged in the first place, not just what's displayed.
- **Filter** — a row of level toggles (only the levels at or above the
    current verbosity are selectable) that lets you show or hide levels in
    the panel without changing the engine's verbosity.
- **Download** (the download icon) — saves the currently filtered logs to a
    `.txt` file, one line per entry with a timestamp.
- **Clear** (the eraser icon) — clears the log list.
- The settings (gear) icon opens **Log Settings**, where you can change the
    maximum number of log entries the panel keeps in memory.

The panel auto-scrolls to the newest entry as logs arrive.

## Error History

The header also carries an **Error History** button (the triangle/circle
icon with a count badge) that's independent of any single run — it
accumulates errors and permission denials across your whole session, not
just the workflow that's currently executing.

Click it to open a dropdown listing each error with a relative timestamp
and its source. Click an entry to see the full error in a modal. **Clear
All** empties the list; the small × next to an individual entry removes
just that one.

<!-- screenshot (#5166): the Error History dropdown open, showing a couple of recorded errors -->

## Saving

The workflow name in the header shows an asterisk (`*`) whenever you have
unsaved changes, whether that's from editing the graph or from a workflow
that's never been saved to disk at all.

- **Save** (`Cmd/Ctrl` + `S`) saves in place. For a workflow that's never
    been saved, this opens the **Save Workflow** dialog so you can choose a
    name.
- **Save As** (`Cmd/Ctrl` + `Shift` + `S`) opens the **Save Workflow As…**
    dialog and writes a new, separate copy under the name you give it.
    Griptape-provided templates always go through Save As — you can't
    overwrite the template itself.
- **Save As New Version** (`Option/Alt` + `Shift` + `S`, on engines that
    support versioned saves) writes the current state as a new versioned
    file (`my_workflow_v002.py`, and so on) and switches you to that new
    version, leaving the previous version untouched on disk.

While a workflow is running, all three save actions are blocked. Trying to
save shows a **Cannot Save While Flow is Running** dialog explaining that
you need to cancel or wait for the run to finish first, with a shortcut
button to cancel the flow right from the dialog.

<!-- screenshot (#5166): the Cannot Save While Flow is Running dialog -->

A **Saving workflow…** overlay appears briefly at the bottom of the screen
while a save is in progress.

### Auto-save

**Settings → Editor Settings → Auto-Save Settings** lets you turn on
automatic saving so you don't have to remember to hit `Cmd/Ctrl+S`. Once
enabled you can set:

- **Auto-Save Interval (seconds)** — how often the editor attempts an
    auto-save.
- **Auto-Save Notifications** — whether a toast confirms each auto-save.

Auto-save only fires when there's an open workflow with unsaved changes
that already exists on disk, and it skips the attempt entirely while the
workflow is running — the same running-workflow guard that blocks manual
saves applies here too, just silently instead of via a dialog.

## Developer Mode

**Settings → Editor Settings → Developer Mode** adds a couple of extras for
understanding *why* the engine is evaluating your workflow the way it is,
rather than just *whether* a node succeeded.

<!-- screenshot (#5166): a node with its Developer Mode panel expanded, showing upstream dependency chips -->

With it enabled, every node gets a collapsible **Developer Mode** strip
that lists its upstream data dependencies — the nodes actually feeding it
data, not control-flow-only connections — each shown with its own
resolution icon (✓ resolved, ↻ resolving, ✗ errored, ○ unresolved).
Clicking a dependency chip pans the canvas to that node, which helps when
you're tracing a stale value back to its source in a large graph.

Developer Mode also adds a **Mark as unresolved** action to the node's
right-click menu. Use it to force a node — and everything downstream of it
— back to the unresolved state without changing anything about the node
itself. That's the fastest way to make the engine re-run a node on the next
Run Workflow or Run To Selected, even though none of its inputs actually
changed (for example, after fixing something external the node depends on,
like a file on disk).
