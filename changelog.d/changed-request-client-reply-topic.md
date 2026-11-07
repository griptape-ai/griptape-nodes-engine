- `RequestClient` now takes its results on a topic of its own, `clients/<client_id>/response`,
  instead of `response`, which every connected editor subscribes to, so other clients no longer get
  them. Pass `response_topic_fn` to choose another topic.
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
