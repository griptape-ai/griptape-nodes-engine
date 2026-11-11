- **Breaking:** `EngineHeartbeatResultSuccess.current_workflow_id` is removed; read
  `open_workflows`. `current_workflow`, `workflow_file_path` and `has_active_flow` describe the
  open workflow only while exactly one is open.
  [#6261](https://github.com/griptape-ai/griptape-nodes-engine/issues/6261)
