- **Breaking:** Manager methods such as `NodeManager.on_get_parameter_value_request(request)` no
  longer act on the open workflow. Send the request with `GriptapeNodes.handle_request(...)`
  instead. See
  [MIGRATION.md](MIGRATION.md#managers-no-longer-have-ambient-workflow-request-methods).
