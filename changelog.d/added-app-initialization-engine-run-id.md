- `AppInitializationComplete` now carries `engine_run_id`, so a client that stays connected while the
  engine restarts, such as one on a relay, can tell it restarted without waiting for a heartbeat.
  Worker engines send their own, so read it only when `is_worker` is false.
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
