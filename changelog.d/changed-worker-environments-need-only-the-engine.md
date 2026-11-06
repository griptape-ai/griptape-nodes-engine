- A library's worker process, including one started inside an environment that
  `worker.command_prefix` prepares, now needs only the `griptape-nodes-engine` package and the
  library's own dependencies, not the Griptape Nodes app. It starts as soon as its library has
  loaded, without waiting for the editor to connect. See
  [Managed Environments](docs/guides/managed_environment.md#workercommand_prefix).
