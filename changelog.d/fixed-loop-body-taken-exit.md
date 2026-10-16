- While, Retry and For Each groups now end, repeat, skip or break the loop according to the branch
  the loop body took. Before, a node on a branch that was not taken could still run, and its exit
  then counted as taken: a group whose body reached "Done" could run again, and a For Each group
  whose body reached "Loop Complete" could break out of the loop.
