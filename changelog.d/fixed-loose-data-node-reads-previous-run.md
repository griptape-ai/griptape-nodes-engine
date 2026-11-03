- A data node with no control connections that reads, directly or through other data nodes, from a
  node on the control path now shows this run's value when the workflow is run again. Before, in
  "parallel" execution mode it could keep the previous run's value, or read a blank value, because
  it ran at the same time as the node feeding it.
