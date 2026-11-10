- **Breaking:** Execution events and streamed progress events now name their workflow in
  `open_workflow_id`, the field a result's envelope uses, instead of `workflow_id`. Clients that
  read `workflow_id` from an `ExecutionEvent` or `ProgressEvent` must read `open_workflow_id`.
  See [MIGRATION.md](MIGRATION.md#execution-events-name-their-workflow-in-open_workflow_id).
