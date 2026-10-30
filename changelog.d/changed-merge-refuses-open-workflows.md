- `MergeWorkflowBranchRequest` now fails while the branch or its source is the open workflow. The
  merge removes the branch and rewrites the source's file, which the open workflow's next save
  would write over. Open another workflow first.
