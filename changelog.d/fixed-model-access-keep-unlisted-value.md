- A node using `ModelAccessComponent` can keep a saved model its list doesn't offer, instead of
  having it replaced with the first choice on load. Pass `keep_unlisted_values=True` and report
  `unlisted_selection_error()`, which names the model, from validation and the execute path.
  `Options` takes `keep_unlisted` for the same behavior on any dropdown.
  [#6461](https://github.com/griptape-ai/griptape-nodes-engine/issues/6461)
