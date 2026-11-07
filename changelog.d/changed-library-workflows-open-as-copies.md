- Opening a Griptape-provided workflow, with any request or by running its file, now opens a new
  unsaved copy with its own `workflow_id`, so the open workflow's id no longer changes when it is
  first saved. Results report the copy's id, such as
  `RunWorkflowFromRegistryResultSuccess.workflow_id`, and the first save is still named after the
  library's file.
  [#6229](https://github.com/griptape-ai/griptape-nodes-engine/issues/6229)
