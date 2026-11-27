- The new `max_workflow_recursion_depth` setting (default 10) limits how deep a Workflow node can
  run its own workflow inside itself. A loop whose body hits the limit stops at that iteration and
  reports the rest as not run (early loop termination does not yet apply to the standard library's
  "Workflow Node").
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
