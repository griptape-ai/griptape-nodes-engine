- Execution events and streamed progress events now name their workflow in `open_workflow_id`, the
  field a result's envelope uses, instead of `workflow_id`. Building an `ExecutionEvent` now
  requires it: pass `open_workflow_id=None` for an event that belongs to no workflow, so an event
  can't reach every client on the session because its workflow was left out.
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
