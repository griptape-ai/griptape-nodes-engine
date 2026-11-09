- A node inside an Isolated Execution group now saves where a `File Output Settings` node wired to
  it from outside the group says, instead of with its own default name, location and existing-file
  policy. The group's run takes a copy of the settings node along with it.
  [#6257](https://github.com/griptape-ai/griptape-nodes-engine/issues/6257)
