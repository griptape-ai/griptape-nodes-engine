- `ForEach Group`, `For Loop Group` and other loop groups show one way in and one way out, `Start Loop`
  and `On Complete`. The second `Flow In` / `Flow Out` pair, which nothing read at run time, is gone, so a
  chain wired through `Flow Out` no longer stops silently at the loop. Every group's control ports now use
  the same `exec_in` / `exec_out` names as other nodes. Workflows saved with connections on the old ports
  load unchanged and the connections move to the new ones.
  [#6250](https://github.com/griptape-ai/griptape-nodes-engine/issues/6250)
