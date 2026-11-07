- `RenameWorkflowRequest`, `MoveWorkflowRequest`, `DeleteWorkflowRequest`, `BranchWorkflowRequest`,
  `MergeWorkflowBranchRequest`, `ResetWorkflowBranchRequest`, `CompareWorkflowsRequest`,
  `GetWorkflowMetadataRequest`, `SetWorkflowMetadataRequest` and `GetWorkflowInfoRequest` take a
  `workflow_id` (and `CompareWorkflowsRequest` a `compare_workflow_id`), so a client can keep naming
  a workflow after it is renamed or moved. Names still work, and a request that gives both fails
  unless they name the same workflow. Each acts on the workflow it names, whichever workflow the
  envelope's `open_workflow_id` names. `CompareWorkflowsResultSuccess` reports both ids.
  [#6227](https://github.com/griptape-ai/griptape-nodes-engine/issues/6227)
