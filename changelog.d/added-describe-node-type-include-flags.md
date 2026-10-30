- `DescribeNodeTypeRequest` takes `include_metadata`, `include_tooltips` and `include_ui_options` to
  leave those parts out, and each parameter now lists its dropdown `options`. A request that leaves
  anything out is cached until libraries change.
  [#5919](https://github.com/griptape-ai/griptape-nodes-engine/issues/5919)
