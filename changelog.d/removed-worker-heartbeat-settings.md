- The `worker.heartbeat_interval_s` and `worker.heartbeat_timeout_s` settings are gone, along with
  their `GTN_CONFIG_WORKER__HEARTBEAT_*` environment variables. Library workers are no longer sent
  heartbeats: an engine knows a worker has stopped the moment its process does. A value left in a
  config file is ignored.
