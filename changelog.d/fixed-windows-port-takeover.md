- On Windows, a second Griptape Nodes process no longer takes a port the first one is already
  serving on. Both processes previously advertised the same address while only one answered, so
  files saved by one — a diagnostics bundle, a generated image — could come back as "not found"
  in the editor. A server that has to move to another port because the one it asked for is taken
  now logs a warning naming both ports.
  [#6186](https://github.com/griptape-ai/griptape-nodes-engine/issues/6186)
