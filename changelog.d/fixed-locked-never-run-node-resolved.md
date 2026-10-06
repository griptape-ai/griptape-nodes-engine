- Locking a node that has never run no longer marks it resolved when a downstream node runs. The
  run skips it with a warning, and unlocking it marks its downstream nodes for re-running, so the
  next downstream run executes it.
  [#5766](https://github.com/griptape-ai/griptape-nodes-engine/issues/5766)
