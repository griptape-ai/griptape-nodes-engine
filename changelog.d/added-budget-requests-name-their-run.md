- `BudgetAccessRequest` and `ReportUsageRequest` take an optional `run_id`, and a node holds the
  run it is executing for as `run_id`. A node that saves `self.run_id` when a call starts and
  passes it on both requests keeps the call's spend on that run even when the report arrives after
  the run has ended, such as a slow call that finishes after Cancel. A request without one counts
  toward the run in progress, as before.
  [#6074](https://github.com/griptape-ai/griptape-nodes-engine/issues/6074)
