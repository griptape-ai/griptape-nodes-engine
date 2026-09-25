- **Breaking:** A request or event field typed `Any` that holds a griptape object, or a value JSON
  has no form for, fails to send with an error naming the payload. It used to be sent as the
  object's `to_dict()` or as its text. Annotate a field that carries parameter values `Value` to
  send them tagged with their type, as described in
  [`get_request_handlers`](docs/development/custom_nodes/advanced_libraries.md#get_request_handlers).
