- **Breaking:** A Save As or versioned save with `continue_in_copy` now closes the open workflow and
  opens the copy in its place, under the copy's own `workflow_id`, so the original file keeps its id.
  Clients get `WorkflowClosedEvent` and `WorkflowOpenedEvent` and should switch to the id in the
  result's `open_workflow`. The save is refused while the workflow is running. See
  [MIGRATION.md](MIGRATION.md#a-save-that-continues-in-the-copy-opens-the-copy).
  [#6302](https://github.com/griptape-ai/griptape-nodes-engine/issues/6302)
