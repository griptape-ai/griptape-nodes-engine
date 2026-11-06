- `AppInitializationComplete` now reports `engine_run_id`, as `EngineHeartbeatResultSuccess` does,
  so a client that reconnects learns the engine restarted without waiting for the next heartbeat.
  Each worker engine sends its own `AppInitializationComplete` with its own id, so read it there
  only when `is_worker` is false.
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
