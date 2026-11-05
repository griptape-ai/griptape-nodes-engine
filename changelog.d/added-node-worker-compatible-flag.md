- Node classes can set `is_worker_compatible = False` so they never run in a worker, neither their
  library's worker nor an Isolated Execution worker.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
