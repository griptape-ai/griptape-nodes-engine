- A library's nodes can now run on another machine running Griptape Nodes, such as a shared GPU
  machine. Turn on the "Distributed execution" beta feature on both machines and assign the library
  to the other machine's engine ID in `distributed_execution.libraries`. The nodes behave as they do
  locally: results, progress, logs, and secret lookups come back to the artist's editor. Artists
  sharing a machine take turns one run at a time, are told their place in line while they wait, and
  the machine frees the previous artist's models as it passes between them
  (`distributed_execution.release`).
