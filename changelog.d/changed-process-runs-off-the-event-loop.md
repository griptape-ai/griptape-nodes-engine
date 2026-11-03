- A node's `process()` now runs on a background thread instead of on the engine's event loop, so a
  slow node no longer stalls heartbeats, cancels, or other requests while it runs. A generator's
  body still runs on the event loop, and nodes that override `aprocess()` are unchanged.
