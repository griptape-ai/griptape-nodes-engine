- Running a node again while another node is still running no longer fails with only the node's
  name as the error. Before, the node also refused every later run until the other node finished,
  and then ran by itself anyway.
  [#5562](https://github.com/griptape-ai/griptape-nodes-engine/issues/5562)
