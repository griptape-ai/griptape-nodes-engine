- Workflow registration at startup, on `RefreshWorkflowRegistryRequest`, and on project switch now
  reads and parses each workflow file's metadata header once instead of twice, and fetches the
  registered library list once per scan instead of once per file. Workspaces holding many workflows
  finish loading sooner.
  [#5770](https://github.com/griptape-ai/griptape-nodes-engine/issues/5770)
