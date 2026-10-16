- A node failing in a parallel run no longer cancels the other nodes running alongside it. Nodes
  that already started finish and show their results, nodes still waiting to start are skipped, and
  the run then reports the failure. Cancelling a run still stops every node.
  [#5893](https://github.com/griptape-ai/griptape-nodes-engine/issues/5893)
