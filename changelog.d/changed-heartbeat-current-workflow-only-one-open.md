- `EngineHeartbeatResultSuccess.current_workflow`, `workflow_file_path` and `has_active_flow`
  describe the open workflow only while exactly one is open; read `open_workflows` otherwise.
  `current_workflow_id` is removed.
  [#6261](https://github.com/griptape-ai/griptape-nodes-engine/issues/6261)
