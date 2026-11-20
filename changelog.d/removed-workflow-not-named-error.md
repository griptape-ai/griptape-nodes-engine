- **Breaking:** `WorkflowNotNamedError` is removed from `griptape_nodes.retained_mode.runtime.binding`.
  The engine stopped raising it, so a node library that imports it fails to load. Remove the import
  and any `except` that names it.
