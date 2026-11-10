- Node authors can mark a parameter deprecated with `deprecation="..."`, a message naming its
  replacement. `DescribeNodeTypeRequest` reports it on each parameter, so MCP clients and the
  built-in chat can leave deprecated inputs out of new workflows.
  [#6288](https://github.com/griptape-ai/griptape-nodes-engine/issues/6288)
