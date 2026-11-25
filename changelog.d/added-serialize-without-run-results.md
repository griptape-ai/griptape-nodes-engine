- `SerializeFlowToCommandsRequest` and `SerializeNodeToCommandsRequest` take `include_run_results`.
  Set it to `false` to leave out what runs produced: node results, outputs, and input values that
  connections fed. `DeserializeFlowFromCommandsRequest` takes `existing_nodes`, which lets the
  commands connect to nodes already in the workflow.
