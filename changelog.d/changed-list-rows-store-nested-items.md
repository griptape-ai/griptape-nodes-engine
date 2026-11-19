- A `ParameterList` that names a concrete item type next to `list`, such as the image inputs of
  `Nano Banana`, now stores a list connected to one of its items as that list's items.
  `get_parameter_value` returns the same flat list as `get_parameter_list_value`, and `max_items`
  counts exactly those items.
