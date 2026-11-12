- End Flow's `result_details` now shows only the current run's `[SUCCEEDED]` or `[FAILED]` status.
  Before, it added another status line on every run when nothing was connected to it, and saving
  kept the extra lines in the workflow.
  [#6304](https://github.com/griptape-ai/griptape-nodes-engine/issues/6304)
