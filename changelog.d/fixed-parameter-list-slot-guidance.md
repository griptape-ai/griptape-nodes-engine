- Agents can fill `ParameterList` inputs such as `items` on Create Image List. A connection
  aimed at the list itself now fails with a message saying to add a slot with
  `AddParameterToNodeRequest(parent_container_name=...)` and connect to the slot it returns. The
  request's tool description and the `griptape-nodes-workflows` skill teach the same steps.
  [#5147](https://github.com/griptape-ai/griptape-nodes-engine/issues/5147)
