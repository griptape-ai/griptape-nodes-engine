- A `ParameterList` with `max_items`, such as the image inputs of `Nano Banana`, now counts the items
  of a connected list toward its limit: a connection that would go over it is refused, adding an item
  is refused once the list is full, and a "Too many items" message appears under the list when a
  connected list later grows past it.
