# Undo and Redo

Undo takes back your last change to a workflow, and Redo puts it back.

| Action | macOS       | Windows / Linux |
| ------ | ----------- | --------------- |
| Undo   | Cmd+Z       | Ctrl+Z          |
| Redo   | Shift+Cmd+Z | Shift+Ctrl+Z    |

The **Undo** and **Redo** buttons at the left of the canvas toolbar do the same. Hover over one to see
what it will take back or put back, such as "Undo Move 'Invert Image'".

While you're typing in a text field, these shortcuts undo and redo your typing in that field instead.

## What counts as one step

Each thing you do is one step, however many nodes it touches: dragging several nodes, pasting,
arranging the graph, or grouping nodes. Typing in a field is one step until you move to another
field.

Changes made by an assistant or another [MCP client](../mcp/external_clients.md) can be undone too,
one request at a time.

## Results

Undo leaves every node the change didn't touch as it is, so it keeps its results and anything it
has loaded, such as a model, and a node you moved back doesn't need to run again. A node whose settings or connections changed is marked to run again, with every node
downstream of it, the same as if you had changed it by hand.

## What isn't undone

- **Running a workflow.** Undo and Redo are unavailable while a workflow is running.
- **Saving.** Undo changes the workflow you're editing, not the saved file. Save again to keep the
    result.
- **Workflow variables.** They keep their current values when you undo or redo.

Each workflow keeps its own history of the last 100 steps, so with several workflows open, Undo
only changes the one you're looking at. Closing a workflow or restarting Griptape Nodes clears its
history. Reloading the editor keeps it.
