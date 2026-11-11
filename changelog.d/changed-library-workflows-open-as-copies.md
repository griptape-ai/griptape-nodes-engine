- Opening a Griptape-provided workflow now opens an unsaved copy with its own `workflow_id`, named
  `unsaved:<id>` until it is first saved. Requests that need its file, such as
  `GetWorkflowRunCommandRequest`, ask you to save it first.
  [#6229](https://github.com/griptape-ai/griptape-nodes-engine/issues/6229)
