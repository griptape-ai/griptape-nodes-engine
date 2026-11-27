- Deprecated nodes are no longer reported as library problems, so a library whose only issue is
  deprecated nodes loads as healthy instead of with problems. `LoadLibraryMetadataFromFileRequest`
  and the startup library status list them in a new `deprecated_nodes` field instead, covering
  nodes deprecated through `metadata.deprecation` and through their own `lifecycle_stage`.
  [#6477](https://github.com/griptape-ai/griptape-nodes-engine/issues/6477)
