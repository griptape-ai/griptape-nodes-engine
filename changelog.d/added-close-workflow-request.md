- `CloseWorkflowRequest` closes the open workflow its `workflow_id` names and leaves any others
  open, whichever workflow the request envelope names. A workflow that is not open succeeds with
  `already_closed`. `ClearAllObjectStateRequest` always closes every open workflow, whatever the
  envelope names.
  [#6205](https://github.com/griptape-ai/griptape-nodes-engine/issues/6205)
