- A result's `open_workflow_id` is now `null` for requests about the engine rather than one
  workflow, such as `SetWorkflowContextRequest`, `CloseWorkflowRequest` or
  `ClearAllObjectStateRequest`, instead of repeating the id the request was sent with. Clients learn
  which workflow those requests opened or closed from `WorkflowOpenedEvent` and
  `WorkflowClosedEvent`. See [Transports](docs/guides/transports.md#getting-results-back).
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
