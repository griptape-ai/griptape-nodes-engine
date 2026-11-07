- `SaveWorkflowRequest` takes `continue_in_copy`. With it set, a Save As or a versioned save keeps
  the open workflow, its `workflow_id` and everything in it on the new file, and registers the
  original file as a separate workflow, so a client no longer reopens the copy and re-runs its file.
  See [Transports](docs/guides/transports.md#open-workflows).
  [#6228](https://github.com/griptape-ai/griptape-nodes-engine/issues/6228)
