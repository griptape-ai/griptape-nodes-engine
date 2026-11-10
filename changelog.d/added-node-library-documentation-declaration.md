- Node and library authors can now declare a `documentation` link in `metadata.declarations`, the
  same way `lifecycle_stage` is declared. A node's own `documentation` url overrides its library's;
  a node with neither has no documentation link. A library can instead declare `url_template` (e.g.
  `".../nodes/{category}/{node}/"`), which the engine interpolates per node, so a library with many
  nodes can give each one a distinct page from a single declaration. The resolved url reaches the
  editor on each node's `library_node_metadata.declarations`.
  [#4998](https://github.com/griptape-ai/griptape-nodes-engine/issues/4998)
