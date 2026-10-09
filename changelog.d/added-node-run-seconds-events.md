- Execution events carry run times. `NodeResolvedEvent` and `NodeErrorEvent` have an optional
  `run_seconds` with how long the node ran, and `ControlFlowResolvedEvent` and
  `ControlFlowCancelledEvent` have one with how long the whole run took, so the editor can show where
  a run spent its time. An End Loop node's or group node's time includes the nodes inside it.
  [#5717](https://github.com/griptape-ai/griptape-nodes-engine/issues/5717)
