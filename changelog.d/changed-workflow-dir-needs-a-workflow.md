- A path that uses `{workflow_dir}` or `{workflow_name}`, including the default project's `{outputs}`,
  `{inputs}` and other `{workflow_dir?:/}` folders, now fails to resolve when no workflow is open,
  instead of quietly resolving under the workspace root where the workflow never looks. Project
  `environment` values that name `{workflow_dir}` are no longer applied, since they apply to the
  whole engine rather than one workflow.
