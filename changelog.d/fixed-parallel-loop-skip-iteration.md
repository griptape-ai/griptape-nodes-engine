- A loop run locally with `run_in_order` off no longer collects the item of a pass that exited
  through `skip_iteration`, so filter loops return the filtered list in parallel too. Loops in an
  isolated or published execution environment still collect skipped passes.
  [#6247](https://github.com/griptape-ai/griptape-nodes-engine/issues/6247)
