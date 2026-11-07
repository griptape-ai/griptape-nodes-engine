- Renaming a saved workflow that isn't open now moves its own file and changes the name in its
  header. It used to save the open workflow's graph under the new name and delete the renamed
  workflow's file. `RenameWorkflowRequest` now also fails when the new name is another workflow's
  file, open or not, and fails for a `workflow_name` no workflow has instead of saving the open
  workflow under the new name.
  [#6227](https://github.com/griptape-ai/griptape-nodes-engine/issues/6227)
