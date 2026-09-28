- A parameter value with no plain-data form is no longer written to saved workflow files, so its
  node runs again when the workflow reopens. It is also left out of copied nodes, exported images,
  and the copy of a loop's or group's nodes that its iterations run, so those copies use the
  parameter's default. Values flowing into the loop from outside it still arrive. A default value
  with no plain-data form on a parameter added to a node is left out the same way. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that are saved, and how to make a class savable.
