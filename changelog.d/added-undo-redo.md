- Workflow edits can be undone and redone with `UndoRequest` and `RedoRequest`. A request is
  undoable when it carries an `undo_group`, and requests sharing one undo as one step. Each MCP tool
  call is its own step. Open workflows report `undo_label` and `redo_label` in their `state`. See
  [Undo and redo](docs/guides/transports.md#undo-and-redo).
