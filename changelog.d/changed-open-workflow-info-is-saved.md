- Every description of an open workflow (in the heartbeat, `ListOpenWorkflowsResultSuccess`,
  `WorkflowOpenedEvent` and results' `open_workflow`) carries `is_saved`, so a client that isn't
  sent file paths can tell saved workflows from unsaved ones.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
