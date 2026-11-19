- `AppGetSessionResultSuccess` now carries `engine_version`, `engine_run_id`, `engine_os`, and
  `is_initializing`, and `SessionHeartbeatResultSuccess` carries `session_id`, `engine_run_id`, and
  `is_initializing`, so a client connected to an engine can follow its state without engine
  discovery.
