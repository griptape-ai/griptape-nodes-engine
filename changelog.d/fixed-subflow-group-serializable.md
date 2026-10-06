- Saving a workflow with a subflow node group no longer warns about values the group passes
  through from parameters marked `serializable=False`. `AddParameterToNodeRequest` takes a
  `serializable` field so a parameter added this way keeps that setting when the workflow reopens.
  [#5073](https://github.com/griptape-ai/griptape-nodes-engine/issues/5073)
