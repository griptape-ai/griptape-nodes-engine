- **Breaking:** The manager methods node libraries called to run a request on the open workflow,
  such as `NodeManager.on_get_parameter_value_request(request)` and
  `FlowManager.on_list_nodes_in_flow_request(request)`, are gone. Send the request with
  `GriptapeNodes.handle_request(...)` instead. The two-argument `on_<name>_request_in_workflow`
  methods are now the bare `on_<name>_request` names, with the same arguments.
  See [MIGRATION.md](MIGRATION.md#managers-no-longer-have-ambient-workflow-request-methods).
