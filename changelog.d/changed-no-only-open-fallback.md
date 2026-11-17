- **Breaking:** Requests that work without a workflow, such as resolving a path or a budget check,
  now act on no workflow when they name none, instead of the only open one. Node code sending one
  from its own thread should use `self.workflow.handle_request(...)`.
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
