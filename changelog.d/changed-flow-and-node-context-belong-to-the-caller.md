- **Breaking:** A request that names no flow acts on the workflow's top-level flow, and a
  `ContextManager().flow()` or `.node()` block reaches only the code that opened it.
  `set_as_new_context` on `CreateFlowRequest` and `CreateNodeRequest` is ignored. `ContextManager`
  no longer has `push_flow`, `pop_flow`, `push_node`, `pop_node`, or element context, and
  `DeserializeFlowFromCommandsRequest` no longer takes `pop_flow_context_after`. See
  [MIGRATION.md](MIGRATION.md#flow-and-node-context-belongs-to-the-caller).
