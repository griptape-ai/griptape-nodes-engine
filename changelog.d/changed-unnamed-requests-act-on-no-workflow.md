- Requests that name no workflow, sent from an MCP session without one or directly to an `Engine`,
  now act on none. Requests that need a workflow fail with `WorkflowNotNamedResultFailure` instead
  of answering as if it were empty. Node library code that reads the current workflow from a thread
  it started itself now finds none. Start the thread with `asyncio.to_thread` to keep the node's
  workflow.
  [#6235](https://github.com/griptape-ai/griptape-nodes-engine/issues/6235)
