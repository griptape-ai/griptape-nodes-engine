- `AdvancedNodeLibrary` subclasses can now override `validate_flow_before_run` to check a whole
  flow, and every node it is about to run, before the flow starts. Returning exceptions, or
  raising one, stops the flow and reports the failures the same way a node's own
  `validate_before_workflow_run` does.
  [#6441](https://github.com/griptape-ai/griptape-nodes-engine/issues/6441)
