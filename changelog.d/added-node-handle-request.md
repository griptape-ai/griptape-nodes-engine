- Nodes have `handle_request(...)` and `ahandle_request(...)`, which send a request through the
  workflow the node is in, including from a thread the node starts. In a node that isn't in a
  workflow yet, a request that needs one fails instead of acting on another workflow.
