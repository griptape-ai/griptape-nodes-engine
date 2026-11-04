- Nodes now have a read-only `workflow` property naming the workflow they were added to, or `None`
  for a node that was never added to one, such as during its `__init__`.
