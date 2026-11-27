- `ParameterList.add_child_parameter()` now accepts an optional `name` for the new row, so the
  first parameter event carries the final name. A name already used in the node raises
  `ValueError` unless `auto_increment=True`, which numbers rows `_1`, `_2`, and so on. Without `name`, rows
  are named as before.
  [#6455](https://github.com/griptape-ai/griptape-nodes-engine/issues/6455)
