- Deleting the current flow with `DeleteFlowRequest(flow_name=...)` no longer leaves it as the
  current flow, which sent later requests to a flow that no longer existed until the engine
  restarted. [#6305](https://github.com/griptape-ai/griptape-nodes-engine/issues/6305)
