- Running a saved workflow from the command line no longer writes `storage_backend` to your config
  file, so it no longer switches the editor's storage backend. `--storage-backend` now applies to
  the run it is passed to instead of the next one, and leaving it out uses the configured backend
  instead of `local`.
