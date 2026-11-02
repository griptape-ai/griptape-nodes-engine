- A node in a library that runs in its own process now gets the engine's real failure result back
  when a request it sends fails, instead of a generic error holding the message text. A budget
  check that Griptape Cloud refuses now arrives there as a `BudgetAccessResultFailure`, as it does
  for a node running in the engine.
  [#5869](https://github.com/griptape-ai/griptape-nodes-engine/issues/5869)
