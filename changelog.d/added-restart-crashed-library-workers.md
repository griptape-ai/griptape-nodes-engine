- A library's worker that crashes after loading, for example killed for running out of memory, is
  now started again by the library's next node, on this engine or the one that runs it. The node
  that was running still fails with the exit code. Before, the library could not run again until it
  was reloaded.
