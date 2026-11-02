- A node in a library that runs in its own process now gets the engine's real failure result back
  when a request it sends fails, instead of a generic error holding the message text. A budget
  check that Griptape Cloud refuses is now recognized there, and raising it stops the run with
  "Budget stopped this run." naming the node and the budgets, as it does for a node in the engine.
  [#5869](https://github.com/griptape-ai/griptape-nodes-engine/issues/5869)
