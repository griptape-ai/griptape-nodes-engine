- **Breaking:** Requests about a saved workflow no longer take its name: `RenameWorkflowRequest`,
  `MoveWorkflowRequest`, `BranchWorkflowRequest`, `MergeWorkflowBranchRequest`,
  `ResetWorkflowBranchRequest`, `CompareWorkflowsRequest`, `GetWorkflowMetadataRequest`,
  `SetWorkflowMetadataRequest`, `GetWorkflowInfoRequest`, `GetWorkflowRunCommandRequest`,
  `SaveSubflowToWorkflowRequest` and `GetWorkflowShapeRequest` drop `workflow_name`,
  `DeleteWorkflowRequest` drops `name`, and `CompareWorkflowsRequest` drops `compare_workflow_name`.
  Name the workflow by `workflow_id` instead. See
  [MIGRATION.md](MIGRATION.md#saved-workflow-requests-take-only-workflow_id).
  [#6302](https://github.com/griptape-ai/griptape-nodes-engine/issues/6302)
