- `DescribeNodeTypeRequest` now reports each parameter's `element_type` and, for a `ParameterList`, its
  `max_items`, so clients can tell multi-input parameters from plain list inputs without creating a node.
  [#5918](https://github.com/griptape-ai/griptape-nodes-engine/issues/5918)
