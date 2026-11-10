- `ForEach Group`, `For Loop Group` and other loop groups show one way in and one way out, `Start Loop`
  and `On Complete`. The second `Flow In` / `Flow Out` pair, which nothing read at run time, is gone, so a
  chain wired through `Flow Out` no longer stops silently at the loop. Workflows saved with connections on
  `Flow In` or `Flow Out` load unchanged and the connections move to `Start Loop` and `On Complete`.
  [#6250](https://github.com/griptape-ai/griptape-nodes-engine/issues/6250)
