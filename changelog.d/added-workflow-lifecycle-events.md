- Every connected client now gets `WorkflowOpenedEvent` when a workflow opens and its graph is built,
  and `WorkflowClosedEvent` when one closes, whichever client sent the request. A close, a clear, a
  library reload and a project switch each send `WorkflowClosedEvent`. Saving a Griptape-provided
  workflow, which continues in its copy under a new id, sends a close of the old id and an open of
  the copy. `WorkflowOpenedEvent`, and the results of requests that open a workflow, including
  `EnsureWorkflowAndFlowResultSuccess` when it opens one, carry `replaced_workflow_id`, the workflow
  closed to open it, so a tab can tell when an open took its workflow's place. See
  [Transports](docs/guides/transports.md#getting-results-back).
  [#6213](https://github.com/griptape-ai/griptape-nodes-engine/issues/6213)
