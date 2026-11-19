- **Breaking:** Requests that act on a workflow, such as `CreateNodeRequest`, now fail with
  `WorkflowNotNamedResultFailure` when they name none, even while only one workflow is open. Send
  `open_workflow_id` on the request, or the `X-Griptape-Workflow-Id` header from MCP clients. Node
  code that sends requests from a thread it starts must use `self.workflow.handle_request(...)`.
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
