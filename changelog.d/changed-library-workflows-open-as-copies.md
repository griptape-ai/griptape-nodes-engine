- Opening a Griptape-provided workflow, with any request or by running its file, now opens a new
  unsaved copy with its own `workflow_id`, so the open workflow's id no longer changes when it is
  first saved. Results report the copy's id, such as
  `RunWorkflowFromRegistryResultSuccess.workflow_id`, and the first save is still named after the
  library's file. Until then the open workflow reports as unsaved: its name is `unsaved:<id>`,
  `is_saved` is false and it has no file path, so requests that need its file, such as
  `GetWorkflowRunCommandRequest` or packaging it, ask you to save it first.
  [#6229](https://github.com/griptape-ai/griptape-nodes-engine/issues/6229)
