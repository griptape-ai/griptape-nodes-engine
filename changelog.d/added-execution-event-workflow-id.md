- Execution events, such as the current node, parameter value updates and a run's end, and streamed
  progress events now have a `workflow_id` naming the open workflow they belong to. A result's
  `open_workflow_id` now names the workflow the request acted on even when the request left it out,
  such as the only open workflow, a run's own workflow, or the workflow a load opened. See
  [Transports](docs/guides/transports.md#getting-results-back).
