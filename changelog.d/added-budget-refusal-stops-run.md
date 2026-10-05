- With Griptape Cloud spending budgets, a node whose call a budget refuses fails with a message
  naming the node and every budget that refused, and the editor's "Run blocked" bar shows it, for
  example: "Budget stopped this run. 'Generate Poster' was blocked by the budget "Marketing Q3".
  Contact your Griptape administrator." A node with its Failure output wired takes that branch, as
  it does for any other error. Sidebar chat replies and the images they generate count against
  the open project's budgets, and a refusal there ends the reply instead. See
  [When a budget stops a run](https://docs.griptapenodes.com/en/stable/guides/editor/running_workflows/#when-a-budget-stops-a-run).
  [#5422](https://github.com/griptape-ai/griptape-nodes-engine/issues/5422)
