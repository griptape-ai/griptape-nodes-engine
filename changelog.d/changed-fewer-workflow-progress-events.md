- Switching projects, starting the engine, and refreshing the workflow registry now send at most ten
  workflow `EngineInitializationProgress` events a second, plus every failure and the final event,
  instead of two per workflow. On a project with 500 workflows, about 1,000 events per scan become
  about 4, so far fewer messages queue up while a project switch waits for its result. Clients that
  counted the per-workflow events receive fewer of them.
