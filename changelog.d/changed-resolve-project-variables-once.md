- Running a workflow spends less time between nodes. Before each node runs, the engine reads the
  project's variables, and it now works out `{workflow_dir}` once per read instead of once for
  every project directory.
