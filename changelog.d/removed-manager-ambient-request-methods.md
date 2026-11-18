- **Breaking:** Node libraries can no longer run a request on the open workflow by calling a manager
  method, such as `NodeManager.on_get_parameter_value_request(request)` or
  `FlowManager.on_list_nodes_in_flow_request(request)`. Those names now belong to the engine's
  dispatch handlers, which require a workflow. Send the request with `GriptapeNodes.handle_request(...)`,
  or from a node with `self.workflow.handle_request(...)`.
  See [MIGRATION.md](MIGRATION.md#managers-no-longer-have-ambient-workflow-request-methods).
