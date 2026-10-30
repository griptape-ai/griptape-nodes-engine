- **Breaking:** `WorkerModeCompatibility`, `SuggestedWorkerMode`, `WorkerMode`, and `WorkerCompatibility`
  are removed from `griptape_nodes.node_library.library_declarations`, and `worker_mode_override` from
  `LibraryRegistration`. A manifest or config file that still carries them keeps loading; the keys are
  ignored. See
  [MIGRATION.md](MIGRATION.md#libraries-no-longer-choose-a-worker).
