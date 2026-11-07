- A result a worker engine sends without a pending request now goes out on its workflow's topic,
  `sessions/<session_id>/workflows/<open_workflow_id>/events`, instead of
  `sessions/<session_id>/response`, so only the clients following that workflow get it. One that
  names no workflow still goes to the session topic. See
  [Following a workflow](docs/guides/transports.md#following-a-workflow).
  [#6240](https://github.com/griptape-ai/griptape-nodes-engine/issues/6240)
