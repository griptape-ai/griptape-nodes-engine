- **Breaking:** A library's manifest no longer picks where its nodes run. A library that declared
  `SuggestedWorkerMode(WORKER)`, or was set to "Isolated" through `worker_mode_override`, now loads
  into the engine process, and its `pip_dependencies` install into `<library>/.venv` on the engine's
  own import path, where they can conflict with the engine's packages. Declare
  `pip_dependencies_exec` to run its `process` in a worker instead. `WorkerModeCompatibility`,
  `SuggestedWorkerMode`, and `worker_mode_override` still parse but have no effect. See
  [MIGRATION.md](MIGRATION.md#libraries-no-longer-choose-a-worker) for the other removals.
  [#5420](https://github.com/griptape-ai/griptape-nodes-engine/issues/5420)
