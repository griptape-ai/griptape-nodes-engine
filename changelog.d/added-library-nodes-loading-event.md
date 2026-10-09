- Creating the first node from a library that has not been used yet no longer waits in silence.
  The import of the library's nodes, which can take a minute for libraries built on large packages
  such as diffusers, is logged at the default level with how long it took. A `LibraryNodesLoading`
  event with status `loading` is sent before the import and `complete` or `failed` after it, so the
  editor can show "Loading <library> nodes for the first time" on the node being placed.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
