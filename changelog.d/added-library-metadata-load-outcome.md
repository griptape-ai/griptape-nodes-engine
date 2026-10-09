- `LoadMetadataForAllLibrariesRequest` now reports how the engine's last attempt to load each
  library went, as `lifecycle_state`, `fitness`, `problems`, and `execution_env_failure`, so a
  library that failed to load can be told apart from one waiting for a refresh.
