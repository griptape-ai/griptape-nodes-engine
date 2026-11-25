- A deprecated node can name the node that replaces it, in any library, with
  `deprecation.replacement` (`library` and `node_type`) in its manifest metadata. Once every library
  has loaded, and whenever one registers or unloads, the engine reports a library problem when the
  replacement's library isn't loaded or has no such node type. Clients see the replacement wherever
  they already get a node's deprecation, the library and workflow deprecation notices name it, and
  the agent's node type list names it when it's available. Deprecated workflow nodes now get the
  same library deprecation notice as Python nodes. Library schema `0.16.0`.
  [#6478](https://github.com/griptape-ai/griptape-nodes-engine/issues/6478)
