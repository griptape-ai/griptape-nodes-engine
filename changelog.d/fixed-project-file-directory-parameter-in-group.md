- `ProjectFileParameter` and `ProjectDirectoryParameter` (used by library authors to add
  project-aware file/directory output parameters to a node) can now be placed inside a
  `ParameterGroup`. Calling `add_parameter()` inside a `with ParameterGroup(...):` block used to
  silently drop the parameter back to the node's root instead of keeping it in the group.
