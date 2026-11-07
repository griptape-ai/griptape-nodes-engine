- Each open workflow that `EngineHeartbeatResultSuccess.open_workflows` lists now carries
  `is_saved`, so a client that isn't sent the workflow's file path can still tell a saved workflow
  from an unsaved one. Every other place that describes an open workflow carries it too:
  `ListOpenWorkflowsResultSuccess`, `WorkflowOpenedEvent`, and the `open_workflow` field of open and
  save results.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
