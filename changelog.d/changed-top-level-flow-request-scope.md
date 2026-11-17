- `GetTopLevelFlowRequest` now acts on a workflow. Naming none answers for the only open workflow,
  as a saved workflow file run as a script asks, and fails with `WorkflowNotNamedResultFailure`
  while none or several are open, instead of answering with no flow.
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
