- A request envelope (`EventRequest`, and each request in an `EventRequestBatch`) now has an optional
  `open_workflow_id` beside `request_id`, naming the open workflow the request runs in.
  `EventManager.handle_request` and `ahandle_request` read it from `result_context`, as they read
  `request_id`, and echo it on the result envelope, where it stays apart from any `workflow_id` in
  the result itself. The engine does not route requests by it yet and never refuses a request for
  carrying one. See [Transports](docs/guides/transports.md#sending-requests).
