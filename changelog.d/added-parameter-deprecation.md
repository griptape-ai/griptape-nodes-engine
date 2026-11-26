- Node authors can mark a parameter deprecated with `deprecation=ParameterDeprecation(...)`, naming
  the parameter that replaces it and an optional message. `DescribeNodeTypeRequest` reports it on
  each parameter, so MCP clients and the built-in chat leave deprecated inputs out of new workflows
  and use the replacement.
  [#6288](https://github.com/griptape-ai/griptape-nodes-engine/issues/6288)
