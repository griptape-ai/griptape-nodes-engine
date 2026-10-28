- `validate_empty_parameter()` takes a `skip_if_connected` option for use in
  `validate_before_workflow_run`, which runs before connections deliver their values. With it, a
  parameter that a connection feeds is not reported as blank.
  [#5889](https://github.com/griptape-ai/griptape-nodes-engine/issues/5889)
