- A Workflow node can now run its own workflow inside itself, for example behind a branch that
  stops the recursion. The new `max_workflow_recursion_depth` setting (default 10) limits how
  deep it can go before an error is raised.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
