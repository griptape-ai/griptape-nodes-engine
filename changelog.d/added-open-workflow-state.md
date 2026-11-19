- `ListOpenWorkflowsRequest`, the heartbeat and `WorkflowOpenedEvent` now report each open
  workflow's `state`: whether it has unsaved changes, whether a run of it is in progress, how many
  runs wait, and why its last run failed. `WorkflowStateChangedEvent` announces each change, so a
  client sees edits and runs that other clients make.
  [#6378](https://github.com/griptape-ai/griptape-nodes-engine/issues/6378)
