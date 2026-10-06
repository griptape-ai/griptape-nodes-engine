- Workflows saved by this version do not open in earlier versions. Saved workflow files store
  parameter values as readable data instead of pickle, and saving an unchanged workflow writes the
  same file each time, so saved workflows diff cleanly. Workflows saved by earlier versions still
  open.
  [#5441](https://github.com/griptape-ai/griptape-nodes-engine/issues/5441)
