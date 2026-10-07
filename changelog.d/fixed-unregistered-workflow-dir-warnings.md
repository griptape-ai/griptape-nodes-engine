- Workflows created with `push_workflow(workflow_name=...)` no longer flood the logs with
  "Optional builtin 'workflow_dir' could not be resolved" warnings before their first save.
  `workflow_dir` now uses the default save folder when no working directory was provided.
