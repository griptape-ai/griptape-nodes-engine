- A request envelope (`EventRequest`, and each request in an `EventRequestBatch`) now has an optional
  `open_workflow_id` beside `request_id`, naming the open workflow the request runs in.
  `EventManager.handle_request` and `ahandle_request` read it from `result_context`, as they read
  `request_id`, and echo it on the result envelope, where it stays apart from any `workflow_id` in
  the result itself. A request that acts on a workflow fails with `WorkflowNotOpenResultFailure`
  without running when its `open_workflow_id` names no open workflow; requests about the engine,
  such as `EngineHeartbeatRequest`, ignore it. See
  [Transports](docs/guides/transports.md#sending-requests).
