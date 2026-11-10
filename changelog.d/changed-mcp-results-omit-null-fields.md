- MCP tool results, including the built-in chat's tools, now leave out fields whose value is
  null, at every level of the result. A missing field means null; `false`, `0`, and empty
  strings, lists, and objects are still sent.
  [#6287](https://github.com/griptape-ai/griptape-nodes-engine/issues/6287)
