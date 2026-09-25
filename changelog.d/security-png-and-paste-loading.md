- Loading a workflow from a PNG, and pasting nodes, no longer unpickle the data unrestricted, which
  let a crafted image run any command when loaded. Data from earlier versions is read by a reader
  that builds only saved value types.
