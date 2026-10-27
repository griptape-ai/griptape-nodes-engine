- **Breaking:** Opening a workflow a library provides, such as a Griptape-provided template, now
  opens an unsaved copy that you save like a new workflow, and the library's own file is never
  changed. `RunWorkflowFromRegistryResultSuccess` and the other `RunWorkflow*` results report the
  copy's `workflow_id`. `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` naming a
  library workflow now fail; open it with `RunWorkflowFromRegistryRequest` instead. Publishing an
  open copy is refused until you save it; before, it published the library's file without your
  edits. `ContextManager.push_workflow()` naming a library workflow raises, and a library workflow's
  file run as a script opens a copy, so its `{workflow_dir}` is the default save folder rather than
  the library's folder. Save As from a copy onto the name of an existing workflow works as it does
  for any other workflow, rather than picking a new name.
