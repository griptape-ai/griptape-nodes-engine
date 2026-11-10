- **Breaking:** Execution and progress events name their workflow in `open_workflow_id`, as results
  do, instead of `workflow_id`. Clients that read `workflow_id` from an `ExecutionEvent` or
  `ProgressEvent` must read `open_workflow_id`. An event a node library builds without it names the
  workflow the library's code is running in.
  See [MIGRATION.md](MIGRATION.md#execution-events-name-their-workflow-in-open_workflow_id).
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
