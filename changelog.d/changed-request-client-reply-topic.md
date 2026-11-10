- `RequestClient` takes its results on its own topic,
  `sessions/<session_id>/clients/<client_id>/response` (`clients/<client_id>/response` without a
  session), instead of the `response` topic every connected editor receives.
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
