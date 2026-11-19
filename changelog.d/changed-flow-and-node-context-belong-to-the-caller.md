- **Breaking:** A request that names no flow now acts on the workflow's top-level flow, because
  requests can no longer make a flow or node current. `set_as_new_context` is ignored, and
  `ContextManager`'s push and pop methods are removed in favor of its `flow()` and `node()` blocks.
  See [MIGRATION.md](MIGRATION.md#flow-and-node-context-belongs-to-the-caller).
