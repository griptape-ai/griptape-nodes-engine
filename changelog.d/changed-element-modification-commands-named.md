- **Breaking:** In `SerializeFlowToCommandsResultSuccess` and
  `ExtractFlowCommandsFromImageMetadataResultSuccess`, each node's `element_modification_commands`
  lists `{"request_type": ..., "request": {...}}` entries, the form `EventRequestBatch` takes,
  instead of each request's fields alone, so each request reads back as its own type.
