- A node's `LogHandlerEvent` now names the workflow the node runs in, in the `open_workflow_id` of
  the `AppEvent` that carries it, so a client with several workflows open shows the line only on
  that workflow's node.
  [#6379](https://github.com/griptape-ai/griptape-nodes-engine/issues/6379)
