- `GriptapeNodes.request` and `GriptapeNodes.try_request` (and `arequest`, `atry_request`) send a
  request and return its own result type, so callers no longer check `isinstance` or `cast`.
  `request` raises `RequestFailedError` when the request fails; `try_request` returns the success,
  the request's failure, or a `DispatchFailure`. Requests name their result types with
  `RequestPayload["FooResultSuccess", "FooResultFailure"]`, and the engine's requests do. See
  [Advanced libraries](docs/development/custom_nodes/advanced_libraries.md).
