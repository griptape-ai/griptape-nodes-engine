- Node and library authors can now declare a `documentation` url in `metadata.declarations`, the
  same way `lifecycle_stage` is declared. A node's own `documentation` declaration overrides its
  library's; a node with neither has no documentation link. The resolved url reaches the editor on
  each node's `library_node_metadata.declarations`.
  [#4998](https://github.com/griptape-ai/griptape-nodes-engine/issues/4998)
