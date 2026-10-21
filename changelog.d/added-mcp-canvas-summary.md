- MCP clients can now call `GetCanvasSummaryRequest` to see a flow's nodes, the values set on them,
  their multi-inputs, and their connections in one compact request, instead of
  `ListNodesInFlowRequest` and a full `GetAllNodeInfoRequest` per node. Outputs, run results, and UI
  state are left out. [#5967](https://github.com/griptape-ai/griptape-nodes-engine/issues/5967)
