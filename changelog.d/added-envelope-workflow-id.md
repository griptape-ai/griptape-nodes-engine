- A request envelope (`EventRequest`, and each request in an `EventRequestBatch`) now has an optional
  `workflow_id` beside `request_id`, naming the open workflow the request runs in.
  `EventManager.handle_request` and `ahandle_request` take it as `workflow_id=` and echo it on the
  result envelope. The engine does not route requests by it yet and never refuses a request for
  carrying one. See [Transports](docs/guides/transports.md#sending-requests).
