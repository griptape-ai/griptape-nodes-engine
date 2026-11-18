- **Breaking:** The manager methods node libraries called to run a request on the open workflow,
  such as `NodeManager.on_get_parameter_value_request(request)` and
  `FlowManager.on_list_nodes_in_flow_request(request)`, are gone. Send the request with
  `GriptapeNodes.handle_request(...)` instead.
  See [MIGRATION.md](MIGRATION.md#managers-no-longer-have-ambient-workflow-request-methods).
