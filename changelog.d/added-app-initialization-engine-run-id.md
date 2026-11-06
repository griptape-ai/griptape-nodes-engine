- `AppInitializationComplete` now carries `engine_run_id`, so a client that reconnects can tell the
  engine restarted. Worker engines send their own, so read it only when `is_worker` is false.
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
