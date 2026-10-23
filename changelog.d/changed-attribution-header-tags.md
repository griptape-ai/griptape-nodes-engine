- Calls through the Griptape Cloud proxy now carry the same attribution tags as direct provider
  calls in the `X-Griptape-Attribution` header: the workflow name, the workflow run's `run_id`, and
  the engine and session, plus the running node's class where the engine can see it. Griptape Cloud
  can then break proxy spend down by workflow and run, not only by project, and total a run's proxy
  and direct spend together. See [What one run cost](https://docs.griptapenodes.com/en/stable/development/custom_nodes/direct_provider_budgets/#what-one-run-cost).
  [#5872](https://github.com/griptape-ai/griptape-nodes-engine/issues/5872)
