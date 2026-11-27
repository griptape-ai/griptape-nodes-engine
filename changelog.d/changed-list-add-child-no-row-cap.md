- `ParameterList.add_child_parameter()` no longer raises `ValueError` once a list has `max_items`
  rows. An empty item counts nothing toward `max_items`, so the limit is checked when an item gets a
  value, and when an item is added from the editor (`AddParameterToNodeRequest`).
