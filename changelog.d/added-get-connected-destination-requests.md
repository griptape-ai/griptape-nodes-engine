- Node libraries can read the destination a connected `File Output Settings` node supplies with
  `GetConnectedFileDestinationRequest`, which works in a worker, and rebuild it with
  `file_destination_from_spec`. `GetConnectedDirectoryDestinationRequest` and
  `GetConnectedFileSequenceDestinationRequest` do the same for directory and file sequence outputs.
  [#5971](https://github.com/griptape-ai/griptape-nodes-engine/issues/5971)
