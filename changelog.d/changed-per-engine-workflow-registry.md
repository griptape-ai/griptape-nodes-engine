- Each engine now keeps its own workflow registry, reached through `engine.workflow_registry`, so
  engines in one process no longer share registered workflows. `WorkflowRegistry` classmethods
  still work and act on the current engine's registry.
