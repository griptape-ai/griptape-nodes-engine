- `EngineHeartbeatResultSuccess` now reports `engine_run_id`, which changes every time the engine
  starts. Workflow ids last only until a restart, so a client that sees a new `engine_run_id` drops
  the workflow ids it kept. Unlike `session_id`, it never survives a restart.
