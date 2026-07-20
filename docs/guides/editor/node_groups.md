# Node Groups

A **group** is a container node that holds other nodes. Every group visually
organizes the nodes inside it, but some group types go further and take over
how those nodes execute — running them together, or running them repeatedly.
This page covers creating, configuring, and running groups. For working with
ordinary nodes, see [Working with Nodes](working_with_nodes.md).

## Creating a group

Select the nodes you want to group (or select nothing, for an empty group you
can drop nodes into later), then:

- Press `Cmd/Ctrl+G` to create a group immediately, using whichever group
    type you've marked as your default.
- Press `Shift+Cmd/Ctrl+G`, or right-click and choose **Create Group**, to
    open the **group type picker** first.

The group type picker lists every group type available from your installed
libraries, searchable the same way the Add Node menu is. Check **Make
default for `Cmd/Ctrl+G`** at the bottom before picking a type to skip the
picker next time. Until you've set a default, `Cmd/Ctrl+G` opens the picker
too.

<!-- screenshot (#5166): the group type picker open, showing the built-in group types with the "make default" checkbox -->

A new group is sized to wrap whatever was selected when you created it, with
a bit of padding. Dropping a node onto an existing group's body adds it to
that group; dragging a child node out removes it.

## Group types

The group types that ship with the standard library fall into two families:

| Type                   | Executes? | What it does                                                                                                                  |
| ---------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Group**              | No        | Purely organizational. Bundles nodes visually with a description field; doesn't run them as a unit or affect execution order. |
| **Subflow Node Group** | Yes       | Runs every node inside it together, as a single unit, in parallel.                                                            |
| **ForEach Group**      | Yes       | Runs its contents once per item in an input list or dictionary.                                                               |
| **For Loop Group**     | Yes       | Runs its contents once per value in a numeric range (start/end/step).                                                         |
| **Retry Group**        | Yes       | Re-runs its contents on failure, up to a configurable number of attempts.                                                     |

A plain **Group** never has a **Run Group** button — it's not executable, so
there's nothing for the engine to run beyond the nodes inside it running on
their own. Every other type in the table is a **subflow group**: it creates a
dedicated subflow to hold its children, and the group node itself becomes a
single executable unit in the outer flow's graph.

### Subflow Node Group

The baseline executable group. When the group runs, every node inside it runs
together in its own subflow, and their outputs are collected back onto the
group's own output parameters. Use it when you want to treat a cluster of
nodes as one step in a larger flow — for readability, or because you want a
single **Run Group** button instead of running each node individually.

### ForEach Group

Iterates over a list or dictionary. Wire a list (or dict) into its **items**
input; the group runs its child nodes once per item, exposing that item on
**current_item** and the item's position on **index** for the child nodes to
consume. A **new_item_to_add** input on the far side collects a value from
each iteration into the group's **results** output list.

**Execution Mode** (in the group's settings) chooses between running
iterations **one at a time** or **all at once**. Only sequential mode
supports the **Skip to Next Iteration** and **Break Out of Loop** control
inputs — they're hidden automatically when you switch to all-at-once, since
there's no "next iteration" to skip to once every iteration is already
running concurrently. A **Testing Mode** toggle in settings lets you run just
one chosen index while you're wiring up the loop body, instead of the whole
list.

### For Loop Group

Same shape as ForEach, but iterates over a numeric range instead of a list —
**start**, **end**, **step**, and an **Include end value** toggle to control
whether the range is inclusive. It shares the same **Execution Mode**,
**results**, **skip**, and **break** machinery as ForEach.

### Retry Group

Re-executes its contents when they fail. Wire the success path from your
child nodes to the group's **Succeeded** control input, and the failure path
to **Failed**. If **Failed** fires and attempts remain (**Max Iterations**,
default 3), the whole group re-runs; if **Succeeded** fires, or attempts run
out, execution continues downstream with **was_successful** reporting which
happened. A **Raise on failure** toggle stops the
workflow with an error when the retry budget runs out, instead of
continuing with **was_successful** set to `false`.

## Wiring connections across the group boundary

Every connection that crosses from outside a subflow group to a node inside
it (or back out) is routed through a **wall parameter** — a parameter that
lives on the group node itself, shown along its left edge (things flowing
in) or right edge (things flowing out). You don't create these by hand: drag
a connection from an inside node's handle to an outside node (or vice versa)
and the wall parameter appears automatically, wired straight through.

<!-- screenshot (#5166): an expanded subflow group showing wall parameters lined up on its left and right edges -->

Behind the scenes each wall parameter is a proxy: the engine deletes the
direct connection, adds a matching parameter on the group, and reconnects
through it in two hops instead of one. If a node with wall connections gets
dragged out of the group, those connections resolve back to a single direct
connection and the now-unused wall parameter disappears; if two nodes inside
the group each need to reach the same outside source, they share a single
wall parameter rather than getting one each.

A plain **Group** doesn't do any of this — it has no subflow to wall off, so
connections just pass straight through its boundary.

## Collapsing and rolling up

Click the chevron in a group's header to **collapse** it. A collapsed group
hides its children and shrinks down to just its wall parameters (or, if it
has none, a single row that says "Group collapsed"). This is the fastest way
to shrink a large group down to its inputs and outputs without losing the
ability to rewire it.

**Roll up** — double-click the group's header, or use the roll-up button
next to the collapse button, when **Node Roll-Up** is enabled in settings —
does something similar without discarding the layout: the group shrinks to
its header height while keeping its current width and its child positions
intact, so expanding it again snaps right back to how you left it.

## Fit to nodes

**Fit to nodes** (`Shift+F` with a single group selected, or the group's
right-click menu / toolbar) resizes the group's box to snugly wrap whatever
nodes are currently inside it, with a fixed padding on every side. Use it
after adding or removing children by hand, when the group's box has drifted
out of sync with what's actually inside it.

## Ungrouping

**Ungroup**, from the group's right-click menu or its toolbar, removes the
group wrapper and leaves its child nodes exactly where they were, converted
from group-relative to absolute canvas positions. For a subflow group, this
also tears down its dedicated subflow and restores every wall connection as
a direct connection between the original nodes — nothing about the
underlying wiring is lost, only the grouping.

Deleting a group node outright (rather than ungrouping it) does the same
thing to its children: they're detached and left on the canvas rather than
deleted along with the group.

## Running a group

Executable group types get a **Run Group** button in their toolbar (visible
when the group is selected), alongside the same collapse and duplicate
controls every group has. Running a group resolves it — and, transitively,
whatever it depends on upstream — the same way running any single node does.

<!-- screenshot (#5166): a selected subflow group with its toolbar showing the Run Group button -->

A subflow group also carries an **Execution Environment** setting (group
settings popover), which chooses where its nodes actually run. It defaults
to **Local Execution** — inside the engine process, same as everything else
on the canvas — with additional options appearing for any installed library
that registers a way to publish and run workflows remotely.

While a group is running, it shows the same **Running** / **Resolved** /
**Unresolved** status pill in its header that ordinary nodes show. A plain
**Group** never shows this pill, since it never runs as a unit.
