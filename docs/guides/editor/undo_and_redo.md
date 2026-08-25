# Undo and Redo

Press `Cmd/Ctrl+Z` to undo your last edit. To redo it, press
`Shift+Cmd/Ctrl+Z`, or `Ctrl+Y` on Windows and Linux. Hold the shortcut down
to keep stepping. The editor keeps the last 100 edits, so you can walk back
through a whole session of work.

While your cursor is in a text field, these shortcuts undo your typing instead
of your canvas, so editing a parameter's text behaves the way it does
everywhere else.

Undo covers editing, not running. Running a workflow, stopping it, or resolving
a single node is never something you undo, and running a workflow doesn't
disturb the edits already on your undo stack.

## What you can undo

Anything that changes the shape or contents of your canvas:

- Adding, deleting, duplicating, or pasting nodes
- Changing a parameter value
- Moving a node, or moving several at once
- Renaming a node
- Connecting two parameters, or deleting a connection
- Resetting a node to its defaults
- Auto-arranging the canvas

Each of these is one step, however you triggered it. Pasting five nodes is a
single undo, not five, and it undoes the same way whether you pasted them,
duplicated them, or added them from a template.

## What you can't undo

Some things live outside the canvas, so an undo has nothing to put back:

- **Workflow variables**, installed **libraries**, and **model or MCP server
    settings**. These belong to your environment, not to one workflow.
- **Adding, removing, or renaming a parameter** on a node. The values on your
    nodes are restored, but the set of parameters a node carries is not.
- **Moving a node into or out of a group.** Group membership isn't part of what
    an undo step records.
- **Saving.** Undo works on the canvas in front of you; it never rewrites a
    file you've already saved.

## How a step is restored

Undo restores your canvas by putting back only what actually changed. Undoing a
value edit on one node leaves every other node exactly as it was, so your
selection, your viewport, and any results already computed on untouched nodes
survive the undo.

Renaming is the one exception. A node is tracked by its name, so undoing a
rename rebuilds that node rather than editing it in place. Anything transient
on it, such as a computed result that hasn't been saved into a parameter, is
lost, the same as it would be on a freshly added node.

## When history is cleared

The undo stack empties when the edits on it no longer describe the canvas
you're looking at:

- Opening, creating, or importing a workflow
- Switching to a different workflow

You also can't undo while a workflow is running. Stop the run first, then undo.

If a step fails to replay, the editor clears the history rather than leave you
with a canvas that's half reverted. This is rare, and it reports what went
wrong when it happens.
