- Setting a parameter that has a hard-limited slider or a min/max range to `None` no longer fails
  with "'>' not supported between instances of 'NoneType' and 'float'". `None` passed over a
  connection into an optional number parameter now goes through instead of stopping the flow.
  [#6063](https://github.com/griptape-ai/griptape-nodes-engine/issues/6063)
