- Every workflow now has a `workflow_id` that the engine gives it while it runs. It stays the same
  when the workflow is saved, renamed, or moved, and when the engine rescans the workspace, but it
  is not saved in the workflow's file, so a restarted engine gives the workflow a new one. Results
  that name a workflow now also report its id, such as `GetWorkflowContextSuccess.workflow_id` and
  `EngineHeartbeatResultSuccess.current_workflow_id`.
