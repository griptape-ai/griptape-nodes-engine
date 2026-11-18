- `RenameWorkflowRequest` now renames an open workflow the way it renames a closed one: it moves the
  file and rewrites the name in its header, instead of saving the workflow to the new file. Edits the
  open workflow has not saved stay unsaved. Renaming an unsaved workflow is now refused; save it
  under the name instead.
  [#6302](https://github.com/griptape-ai/griptape-nodes-engine/issues/6302)
