- Nodes can raise `NodeError` from `griptape_nodes.exe_types.core_types` to attach labelled values
  such as a request ID, the provider's response body, and up to three links, instead of putting
  them in the message text. A link is a web page or, when it starts with `#`, a place in the
  editor, such as the API key the node needs. They reach the editor in `NodeErrorEvent.error`, also
  when the node runs in a worker. See
  [Writing Error Messages](docs/development/custom_nodes/error_handling.md#writing-error-messages).
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733)
