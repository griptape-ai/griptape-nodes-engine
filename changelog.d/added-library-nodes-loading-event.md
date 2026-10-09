- Creating the first node from a library that has not been used yet now sends a
  `LibraryNodesLoading` event with status `loading` before the engine imports the library's nodes,
  and `complete` or `failed` when the import ends. The import can take a minute for libraries built
  on large packages such as diffusers. The editor can use the event to show "Loading <library>
  nodes for the first time" on the node being placed instead of a blank placeholder.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
