- Requests about a saved workflow, such as `RenameWorkflowRequest`, `DeleteWorkflowRequest` and
  `PublishWorkflowRequest`, take a `workflow_id`, so a client can keep naming a workflow after it is
  renamed or moved. A request that gives both an id and a name fails unless they agree. See
  [Transports](docs/guides/transports.md#open-workflows) for the full list.
  [#6227](https://github.com/griptape-ai/griptape-nodes-engine/issues/6227)
