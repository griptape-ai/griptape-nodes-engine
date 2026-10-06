- A Workflow node that points back at the workflow containing it, directly or through other
  workflows such as a saved template, no longer re-imports that workflow until the engine stops.
  The import is refused with an error naming the workflow that contains itself.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
