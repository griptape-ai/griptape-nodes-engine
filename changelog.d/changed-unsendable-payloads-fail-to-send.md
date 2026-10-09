- **Breaking:** A request or event field typed `Any` that holds a griptape object, or a value JSON
  has no form for, fails to send with an error naming the payload. It used to be sent as the
  object's `to_dict()` or as its text. A result that fails to send this way answers its request
  with a generic failure naming the reason, instead of leaving the request without a reply.
  Annotate a field that carries parameter values `Value` so they send, as described in
  [`get_request_handlers`](docs/development/custom_nodes/advanced_libraries.md#get_request_handlers).
  See [MIGRATION.md](MIGRATION.md#parameter-values-keep-their-type).
