- **Breaking:** Results of the requests that build a workflow while it opens now report
  `altered_workflow_state` as their handlers set it, instead of always `false`. To tell whether a
  workflow has unsaved changes, read `has_unsaved_changes` in its `OpenWorkflowInfo.state` and
  `WorkflowStateChangedEvent`.
