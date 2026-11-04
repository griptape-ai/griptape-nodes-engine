- `RefreshProjectTemplatesRequest` re-checks every path in `projects_to_register` and loads the
  projects that have become reachable since the engine started, such as projects on a network drive
  that mounted late, without restarting the engine.
  [#6134](https://github.com/griptape-ai/griptape-nodes-engine/issues/6134)
