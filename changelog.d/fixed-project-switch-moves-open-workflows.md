- **Breaking:** Switching projects (`SetCurrentProjectRequest`) now closes every open workflow, so a
  workflow left open, such as in another tab, no longer saves its outputs into the new project's
  folders. Reopen the workflows you need after switching. Switching now fails while a workflow is
  running, so wait for the run to finish or cancel it first.
