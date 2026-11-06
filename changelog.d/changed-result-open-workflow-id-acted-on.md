- A result's `open_workflow_id` now names the workflow the request acted on even when the envelope
  named another: the workflow an open opened, or `null` for a request about the engine such as
  `ClearAllObjectStateRequest`. Only a request refused for the workflow it named still echoes the
  envelope's id. See [Transports](docs/guides/transports.md#getting-results-back).
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
