- `OpenWorkflowRequest` opens a workflow by `workflow_id` or `workflow_name`, or a new unsaved one
  when it names neither, and builds a saved workflow's file into it. A workflow that is already
  open comes back as it is, with `already_open` set. It never closes a workflow: it fails with
  `AnotherWorkflowOpenResultFailure` while another is open, and with
  `WorkflowNotFoundResultFailure` for an id or name the engine doesn't know, such as an unsaved
  workflow that was closed or lost to a restart.
  [#6221](https://github.com/griptape-ai/griptape-nodes-engine/issues/6221)
