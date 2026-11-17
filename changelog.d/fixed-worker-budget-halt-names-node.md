- A node in a library that runs in its own process now names itself when Griptape Cloud refuses its
  `BudgetAccessRequest` and it raises the refusal: the run stops with "Budget stopped this run.
  'Node' was blocked by ...", as for a node in the engine, instead of "The next call was blocked by".
  [#5869](https://github.com/griptape-ai/griptape-nodes-engine/issues/5869)
