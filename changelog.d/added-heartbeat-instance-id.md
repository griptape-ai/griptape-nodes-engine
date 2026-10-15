- `EngineHeartbeatResultSuccess` now reports `instance_id`, a random id generated each time the
  engine starts. `engine_id` and `session_id` stay the same across a restart, so a client can
  compare `instance_id` to tell that the engine restarted and reload what it got from the old one.
  [#5900](https://github.com/griptape-ai/griptape-nodes-engine/issues/5900)
