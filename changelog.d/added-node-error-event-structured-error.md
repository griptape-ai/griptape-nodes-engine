- `NodeErrorEvent` has an optional `error` field that holds a node failure in parts: the node's own
  message without the engine's "Attempted to execute node" wrapping or a leading node name, the
  exception type, one message per problem when a node fails validation, and anything the node
  attached with `NodeError`. When the engine wrote the failure itself, such as a worker that stopped
  responding, `error.message` is the engine's text. `ExecuteNodeResultFailure.error` carries the
  same parts back from the process the node ran in. A provider response a node printed at the end
  of its message, such as "Full API response:" followed by a dict, is sent as `error.response`
  and left out of `error.message`, except for the provider's own explanation, which stays in the
  message. `error_message` is unchanged.
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733),
  [#5811](https://github.com/griptape-ai/griptape-nodes-engine/issues/5811)
