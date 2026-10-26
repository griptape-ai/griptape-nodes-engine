- **Breaking:** A path that uses `{workflow_dir}` or `{workflow_name}`, including the default
  project's `{outputs}`, `{inputs}` and other `{workflow_dir?:/}` folders, now fails to resolve when
  the request has no workflow, for example when nothing is open, instead of quietly resolving under
  the workspace root. Request clients that resolve such paths must send the request from a workflow
  tab or address it to a workflow, or use a macro without `{workflow_dir}`, such as
  `{workspace_dir}`. A request that supplies its own `workflow_dir` value still resolves with it.
  Project `environment` values are applied one by one: a value that names `{workflow_dir}` or
  `{workflow_name}` is skipped with a warning, since environment values apply to the whole engine
  rather than one workflow, and the other values are still applied.
