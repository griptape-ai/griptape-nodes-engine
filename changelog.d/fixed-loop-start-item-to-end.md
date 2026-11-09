- A loop with nodes in its body no longer collects an empty list when the start node's
  `current_item` or `index` is wired straight into the end node's `new_item_to_add`. A ForEach or
  For Loop group whose own `current_item` or `index` is wired into its own `new_item_to_add` now
  runs and collects the items instead of being skipped by the run.
  [#6238](https://github.com/griptape-ai/griptape-nodes-engine/issues/6238)
