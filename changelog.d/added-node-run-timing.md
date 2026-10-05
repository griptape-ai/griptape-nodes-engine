- A "Node run timing" beta feature writes how long each node took to run to the engine log, as
  `TIME TO RUN: <seconds> s for '<node name>' (<node type>)`. When a workflow run finishes, fails,
  or is cancelled, it also logs a `RUN SUMMARY` with the total time, the nodes grouped by which ran
  in parallel, slowest first, and the nodes inside each group or loop. The
  `logging.log_node_run_timing` setting, on by default, turns this logging off without turning off
  the beta feature.
  [#5717](https://github.com/griptape-ai/griptape-nodes-engine/issues/5717)
