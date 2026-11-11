- **Breaking:** Results a worker engine sends without a pending request go to their workflow's topic,
  `sessions/<session_id>/workflows/<open_workflow_id>/events`, instead of
  `sessions/<session_id>/response`. Follow each workflow's topic to get them. See
  [Following a workflow](docs/guides/transports.md#following-a-workflow).
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
