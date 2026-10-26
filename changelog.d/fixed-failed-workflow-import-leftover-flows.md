- When a workflow fails to import into a `Workflow` node or into the editor, the flows and nodes it
  had already built are now removed. They no longer stay in your workflow or get written into the
  saved file, and trying again gives the same names instead of higher-numbered ones each time.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
