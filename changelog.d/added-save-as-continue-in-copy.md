- `SaveWorkflowRequest` with `continue_in_copy` keeps the open workflow, and its `workflow_id`, on
  the new file after a Save As or versioned save, and registers the original file as a separate
  workflow. See [Transports](docs/guides/transports.md#open-workflows).
  [#6228](https://github.com/griptape-ai/griptape-nodes-engine/issues/6228)
