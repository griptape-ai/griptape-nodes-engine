- Scanning a project's workflows (at startup, on a project switch, or on a registry refresh) no
  longer sends a `RegisterWorkflowRequest` result to clients for each workflow. With 500 workflows
  these queued ahead of the switch's own result and held it back about a second. Progress still
  arrives as `EngineInitializationProgress` events, and each result is still logged at DEBUG.
