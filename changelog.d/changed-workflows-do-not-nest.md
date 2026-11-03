- **Breaking:** `RunWorkflowFromRegistryRequest` with `run_with_clean_slate=False` now fails while
  a workflow is open, instead of loading the second workflow into the open one, since only one
  workflow is open at a time. Clear the open workflow first with `ClearAllObjectStateRequest`, or
  send `run_with_clean_slate=True`. Likewise, `GriptapeNodes.ContextManager().push_workflow()` and
  its `workflow()` block now refuse while a workflow is open.
