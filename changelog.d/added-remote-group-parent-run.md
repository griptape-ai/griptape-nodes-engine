- Published workflows now accept `--parent-run-id`, and `LocalWorkflowExecutor` takes
  `parent_run_id`, so a node group run in a remote execution environment can count its spend toward
  the run that started it. A publisher library opts in by passing its published-workflow node's
  `self.run_id` when it starts the remote run. See
  [What one run cost](https://docs.griptapenodes.com/en/stable/development/custom_nodes/direct_provider_budgets/#what-one-run-cost).
  [#6075](https://github.com/griptape-ai/griptape-nodes-engine/issues/6075)
