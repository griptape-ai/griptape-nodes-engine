- A node's `validate_before_node_run` now runs in a worker thread instead of on the engine's event
  loop, so a slow check, such as reading a large or remote file, no longer freezes the editor and
  other flows while it runs. The node still waits for its own checks before it runs, and a failed
  check stops the run as before.
  [#5963](https://github.com/griptape-ai/griptape-nodes-engine/issues/5963)
