- **Breaking:** A library's manifest no longer picks where its nodes run. `WorkerModeCompatibility`,
  `SuggestedWorkerMode`, and the `worker_mode_override` config key still parse but have no effect;
  declare `pip_dependencies_exec` to run a library's `process` in a worker subprocess.
