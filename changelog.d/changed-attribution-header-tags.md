- Calls through the Griptape Cloud proxy now carry the same attribution tags as direct provider
  calls in the `X-Griptape-Attribution` header: the workflow name, the workflow run's `run_id`, and
  the engine and session, plus the running node's class. Griptape Cloud can then break proxy spend
  down by workflow and run, not only by project, and total a run's proxy and direct spend together.
  A node library gets these tags when it asks for the header in the node's own context; a header
  asked for from a bare thread carries only the project, engine and session. A chat turn with the
  assistant sends no run, workflow or node tags, so it never counts toward a run. See
  [What one run cost](https://docs.griptapenodes.com/en/stable/development/custom_nodes/direct_provider_budgets/#what-one-run-cost).
  [#5872](https://github.com/griptape-ai/griptape-nodes-engine/issues/5872)
