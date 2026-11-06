- `AppInitializationComplete` now reports `engine_run_id`, as `EngineHeartbeatResultSuccess` does,
  so a client that reconnects learns the engine restarted without waiting for the next heartbeat.
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
