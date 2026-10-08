- `GTN_ENGINE_CONFIG_DIR`, `GTN_ENGINE_DATA_DIR` and `GTN_ENGINE_STATE_DIR` move the engine's
  configuration, data and state directories without changing `XDG_*` for programs the engine
  starts, such as `uv`. Each names the final directory and must be an absolute path; relative
  values are ignored.
