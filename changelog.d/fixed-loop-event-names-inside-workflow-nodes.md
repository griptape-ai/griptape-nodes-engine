- `NodeStartProcessEvent` and `NodeFinishProcessEvent` for a loop body inside a Workflow node in a
  loop group now name the body node and its flow as they name the workflow's other nodes. This
  applies to Workflow nodes that a node library generates from its declared workflows, not the
  standard library's "Workflow Node".
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
