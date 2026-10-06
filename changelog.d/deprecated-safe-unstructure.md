- `safe_unstructure` from `griptape_nodes.retained_mode.events.event_converter` is deprecated and
  will be removed in a later release. Use `encode_value` from `griptape_nodes.serialization.values`
  to turn a parameter value into plain data. It no longer uses a griptape object's `to_dict()`, or
  falls back to a value's text.
