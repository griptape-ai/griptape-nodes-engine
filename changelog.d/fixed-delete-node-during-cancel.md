- Deleting a node while its workflow is running, or deleting two nodes at the same time, no longer
  lets other requests see the node being deleted as the current node, or mix up which node each
  delete is working on.
  [#5665](https://github.com/griptape-ai/griptape-nodes-engine/issues/5665)
