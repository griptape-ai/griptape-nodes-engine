- On Windows, a second Griptape Nodes process no longer takes a port the first one is already
  serving on. Both processes previously advertised the same address while only one answered, so
  files saved by one — a diagnostics bundle, a generated image — could come back as "not found"
  in the editor.
  [#6186](https://github.com/griptape-ai/griptape-nodes-engine/issues/6186)
