- `BudgetAccessRequest` and `ReportUsageRequest` take an optional `run`, and a node holds the run
  it is executing for as `self.run_attribution`: the run's id, workflow and project. A node that
  saves `self.run_attribution` when a call starts and passes it as `run` on both requests keeps the
  call's spend on that run, workflow and project's budgets even when the report arrives after the
  run has ended, such as a slow call that finishes after Cancel. A request without one counts
  toward the run, workflow and project open when it arrives, as before.
  [#6074](https://github.com/griptape-ai/griptape-nodes-engine/issues/6074)
