- `ListNodeTypesInLibraryRequest` takes `include_metadata` to also return each node type's category,
  display name, description, and deprecation. MCP clients can survey every library's node types in
  one `EventRequestBatch` instead of reading library manifests off the engine's disk. It reads library
  metadata only, so it loads no node code.
  [#5962](https://github.com/griptape-ai/griptape-nodes-engine/issues/5962)
