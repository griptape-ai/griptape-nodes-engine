- `pickle_control_flow_result` on `StartFlowRequest`, `StartFlowFromNodeRequest`,
  `StartLocalSubflowRequest`, `SaveWorkflowRequest`, `SaveWorkflowFileFromSerializedFlowRequest`,
  `PublishWorkflowRequest`, and the workflow executors, and the `--pickle-control-flow-result` CLI
  flag, have no effect. Flow results always travel as plain data. They will be removed in a later
  release.
