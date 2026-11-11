- **Breaking:** Unsolicited results from a worker engine now go to their workflow's topic,
  `sessions/<session_id>/workflows/<open_workflow_id>/events`, instead of
  `sessions/<session_id>/response`.
  See [Following a workflow](docs/guides/transports.md#following-a-workflow).
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
