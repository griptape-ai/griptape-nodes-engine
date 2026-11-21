- Wiring a `Subflow Node Group`'s `On Start` to a node other than the one it would pick on its own
  now actually sets where the group enters, both for its own run and for its packaged run under
  Isolated or library execution. Previously the wiring had no effect either way.
  [#6210](https://github.com/griptape-ai/griptape-nodes-engine/issues/6210)
