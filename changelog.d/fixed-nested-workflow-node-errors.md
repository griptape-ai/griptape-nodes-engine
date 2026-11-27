- When a node fails inside nested Workflow nodes, the error now shows that node's message once,
  names it and how many nested Workflow nodes it was inside, and lists the nodes in between,
  including groups and one-at-a-time loops, in a "Failed node path" field.
  When a one-at-a-time loop's iterations fail for different reasons, the path ends at the loop
  and the error lists every reason.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
