- A node's `process()` now runs on a background thread, along with the generator code between its
  yields, so a slow `process()` no longer freezes the editor or delays other nodes. Nodes running
  in parallel no longer take turns inside `process()`; guard any state they share with a lock.
