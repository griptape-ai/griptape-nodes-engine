- **Breaking:** Events and payloads no longer serialize themselves. `json()`, `dict()`,
  `strict_json()`, `failure_json()` and `from_dict()` on events, and `to_json()` on payloads, are
  removed. Use the converters in `griptape_nodes.retained_mode.events.converters`: `engine` keeps
  parameter values' types for another engine process, `client` sends them as plain JSON.
  See [MIGRATION.md](MIGRATION.md#events-are-serialized-by-converters).
