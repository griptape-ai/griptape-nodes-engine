- `DeserializeFlowFromCommandsRequest` and `SaveWorkflowFileFromSerializedFlowRequest` sent as JSON
  read their nested node, connection, and parameter commands back as commands, instead of as plain
  dicts the request could not use.
