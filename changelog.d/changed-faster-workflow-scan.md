- Switching projects, starting the engine, and refreshing the workflow registry scan a project's
  workflows faster. On a workspace with 3,800 `.py` files, a scan takes about 0.8 seconds instead of
  1.6 to 2.1. Workflow progress now arrives as at most ten `EngineInitializationProgress` events a
  second, plus every failure and the final event, instead of two per workflow. Each
  `RegisterWorkflowRequest` result from the scan is no longer sent to clients, only logged at DEBUG.
  Together these had queued about 1,500 messages ahead of a project switch's own result.
