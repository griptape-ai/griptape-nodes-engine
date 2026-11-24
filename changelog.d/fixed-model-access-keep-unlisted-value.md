- A node using `ModelAccessComponent` can keep a saved model its list doesn't offer, instead of
  having it replaced with the first choice on load. Pass `keep_unlisted_values=True`; the
  parameter shows a "Model Not Available" badge naming the model, and the existing selection gate
  (`query_for_denial` / `raise_if_denied` / `selection_denial` / `raise_if_selection_denied`)
  fails the run naming it.
  [#6461](https://github.com/griptape-ai/griptape-nodes-engine/issues/6461)
