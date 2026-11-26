- A deprecated node can name the nodes that replace it with `deprecation.replacements` in its
  manifest metadata, each a `node_type` plus a `library` when it's in another library. Once every
  library has loaded, and whenever one registers or unloads, the engine reports a library problem
  when a replacement's library isn't loaded or has no such node type. The workflow deprecation notice
  names the replacements by display name, and the agent's node type list names the ones it can
  create. Library schema `0.16.0`.
  [#6478](https://github.com/griptape-ai/griptape-nodes-engine/issues/6478)
