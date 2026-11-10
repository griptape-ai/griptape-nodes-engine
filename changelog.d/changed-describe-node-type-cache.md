- `DescribeNodeTypeRequest` caches each node type's description until libraries change, so describing
  a whole library again takes about 100 ms instead of creating every node type again.
  [#5919](https://github.com/griptape-ai/griptape-nodes-engine/issues/5919)
