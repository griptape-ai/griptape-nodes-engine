- A saved workflow run without `--project-file-path` now fails when its configured project (the
  `project_file` setting, else the workspace's `griptape-nodes-project.yml`) cannot be activated,
  instead of running on system defaults.
  [#6214](https://github.com/griptape-ai/griptape-nodes-engine/issues/6214)
