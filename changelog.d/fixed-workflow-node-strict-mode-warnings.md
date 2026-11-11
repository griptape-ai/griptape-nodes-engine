- Running a Workflow node no longer logs strict-mode warnings that blame it for the
  `GriptapeNodes.ContextManager()` calls in the workflow file it runs. The engine now runs Workflow
  nodes the way it runs node groups and loops, outside any node's run.
  [#6303](https://github.com/griptape-ai/griptape-nodes-engine/issues/6303)
