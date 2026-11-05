- `File`, `FileDestination`, `Directory` and `FileSequence` now keep the workflow they were built in,
  or take one as `workflow`, so node code that writes from a `threading.Thread` or thread pool
  resolves `{outputs}` in the node's workflow.
