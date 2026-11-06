- **Breaking:** `RunWorkflowFromRegistryRequest` no longer has a `run_with_clean_slate` field. It
  closes the workflow `replaces_workflow_id` names, or the open workflow when that is left out, as
  `RunWorkflowFromScratchRequest` does. The engine ignores the field when a client still sends it,
  so `run_with_clean_slate=False` no longer fails while a workflow is open: the open workflow
  closes first. A client that relied on that refusal should check what is open first.
  [#6216](https://github.com/griptape-ai/griptape-nodes-engine/issues/6216)
