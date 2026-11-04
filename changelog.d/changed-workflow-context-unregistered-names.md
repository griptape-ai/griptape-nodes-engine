- **Breaking:** `SetWorkflowContextRequest` and `EnsureWorkflowAndFlowRequest` now fail for a
  `workflow_name` that no registered workflow has, instead of opening a blank workflow under that
  name. To start a new workflow, leave `workflow_name` out and pass `display_name`.
  `ContextManager.push_workflow(workflow_name=...)` with such a name opens a new unsaved workflow
  with that display name and returns its `unsaved:<id>` name. `push_workflow(file_path=...)` with a
  path that has no file opens a new unsaved workflow in that file's folder, and
  `get_current_workflow_file_path()` returns `None`.
