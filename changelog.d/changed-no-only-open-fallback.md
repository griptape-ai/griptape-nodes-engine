- **Breaking:** Requests that work without a workflow, such as resolving a path or a budget check,
  now act on no workflow when they name none, instead of the only open one. For MCP clients and
  scripts, `EnsureWorkflowAndFlowRequest` and `RunWorkflowWithCurrentStateRequest` naming no
  workflow now open a new one instead of building into the workflow the editor has open. Node code
  sending one from its own thread should use `self.handle_request(...)`.
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
