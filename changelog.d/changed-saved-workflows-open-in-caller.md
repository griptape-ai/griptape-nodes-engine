- **Breaking:** A saved workflow file's `build_workflow()` no longer opens a workflow. It builds into
  the workflow its caller opened, and `aexecute_workflow()` opens the file when it runs on its own.
  Code that runs a saved file by awaiting `build_workflow()` directly must open the workflow first
  with `GriptapeNodes.ContextManager().push_workflow(file_path=...)`, or call
  `GriptapeNodes.WorkflowManager().run_workflow()`, which opens it when none is open. Files saved
  before keep working unchanged.
