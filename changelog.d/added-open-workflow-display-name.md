- Each open workflow that the heartbeat, `ListOpenWorkflowsRequest`, `WorkflowOpenedEvent` and
  results' `open_workflow` report now carries `display_name` and `is_saved`, so a client can label
  it and tell saved workflows from unsaved ones without asking for its metadata or file path.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
