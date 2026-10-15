- Nodes that call a model provider directly, rather than through Griptape Cloud, can now take part
  in Griptape Cloud spending budgets. `BudgetAccessRequest` asks Cloud before the call and fails
  with the same "Budget stopped this run." message when a budget refuses it; if Cloud cannot be
  reached the call goes ahead. `ReportUsageRequest` reports the call's cost afterwards, in the
  background. See [Budgets for Direct Provider Calls](https://docs.griptapenodes.com/en/stable/development/custom_nodes/direct_provider_budgets/).
  [#5422](https://github.com/griptape-ai/griptape-nodes-engine/issues/5422)
