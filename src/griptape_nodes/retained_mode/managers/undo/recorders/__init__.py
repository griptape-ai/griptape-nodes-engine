"""Per-domain undo recorders.

Each module holds one domain's knowledge of how to reverse its own request types (e.g. `node`
for node/parameter requests, `flow` for connection requests). Recorders build on the vocabulary
in `undo.core` and are registered with the `UndoManager` by the owning domain manager; nothing in
the undo core imports this package.
"""
