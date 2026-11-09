- A Workflow node that points back at the workflow containing it, directly or through other
  workflows such as a saved template, no longer re-imports that workflow until the engine stops.
  It now stops with an error naming the workflow once it is nested deeper than
  `max_workflow_recursion_depth`.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
