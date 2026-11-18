- A workflow's `workflow_id` is now saved in its file, so it stays the same across engine restarts,
  renames and moves. Saved workflows link to other workflows by id (`branched_from`,
  `workflows_referenced` and referenced subflow imports), so renaming a referenced workflow no longer
  breaks the link. A file copied outside the editor gets a new id, and a file moved outside the
  editor keeps its own. Files saved by earlier versions open as before and are written with ids on
  their next save. Workflows are now saved with schema 0.22.0.
  See [MIGRATION.md](MIGRATION.md#workflow-ids-are-saved-in-the-workflow-file).
  [#6302](https://github.com/griptape-ai/griptape-nodes-engine/issues/6302)
