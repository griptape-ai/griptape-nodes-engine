- Connecting an output to a `ParameterList` that would go over its `max_items` now fails with an
  error naming the list and the limit, and no connection is made. This includes lists such as the
  image inputs of `Nano Banana`, where a connected list now counts as its items. A source that has
  not run yet fails on run if the limit is exceeded.
