- `NodeStartProcessEvent` is sent again before every node runs, and `NodeFinishProcessEvent` after,
  for every node the engine runs: regular nodes, nodes that run in a worker, group and loop nodes,
  and the nodes inside them. Both now carry `node_type`, `specific_library_name`, `flow_name`,
  `execution_id`, and `parent_execution_id`, and `NodeFinishProcessEvent` says how the run ended
  (`outcome`, `error_message`, `error`). Loop body nodes are named as on the canvas.
  [#5779](https://github.com/griptape-ai/griptape-nodes-engine/issues/5779)
