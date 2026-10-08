- Unlocking a node that was locked before it ever ran now lets the next downstream run execute it.
  A run passing through a locked node marks it resolved without running it, and that state used
  to outlive the unlock, so the node was skipped until it was run directly.
  [#5766](https://github.com/griptape-ai/griptape-nodes-engine/issues/5766)
