- With the `isolated_execution_in_worker` beta on, nodes under a group or loop set to Isolated
  Execution run again. The general-purpose worker now loads every library at startup, where before
  the engine refused it with "it did not finish loading libraries".
  [#6481](https://github.com/griptape-ai/griptape-nodes-engine/issues/6481)
