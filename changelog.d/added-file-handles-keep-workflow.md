- `File`, `FileDestination`, `Directory`, `FileSequence` and their destinations now keep the workflow
  they were built in, and take a `workflow` argument to name one. A file a node builds, including
  with `ProjectFileParameter.build_file()` called from a `threading.Thread` or a thread pool, now
  resolves `{outputs}` and the other project macros in the node's workflow.
