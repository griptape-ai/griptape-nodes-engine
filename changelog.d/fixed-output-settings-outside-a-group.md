- A node now saves where a `File Output Settings` node wired to it says when the wire crosses a
  group boundary: the node inside the group and the settings node outside it, the other way round,
  or each in its own group. Before, it saved with its own default name, location and
  existing-file policy.
  [#6146](https://github.com/griptape-ai/griptape-nodes-engine/issues/6146)
