- The built-in chat now starts every message with the available node types and the current flow in its
  instructions, and is told to plan the whole change and apply it in one `EventRequestBatch` call
  instead of starting with discovery calls. Saved conversations no longer keep a copy of the
  instructions with every model request.
  [#5909](https://github.com/griptape-ai/griptape-nodes-engine/issues/5909)
