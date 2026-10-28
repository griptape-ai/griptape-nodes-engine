- **Breaking:** A node whose library runs in a separate process now runs there on a copy loaded the
  way a saved workflow reopens, with the same parameters and types as the node in the editor,
  including parameters added in the editor, by requests, and by its value hooks. `after_value_set`
  no longer runs in that process, and nothing the node's code sends while the copy loads reaches the
  workflow. A node whose `process` relied on state its `after_value_set` set needs to keep that
  state in a parameter; see
  [Node Isolation with Workers](docs/development/custom_nodes/node_isolation_with_workers.md#the-rules-that-keep-a-worker-identical-to-running-in-process).
  [#5955](https://github.com/griptape-ai/griptape-nodes-engine/issues/5955)
