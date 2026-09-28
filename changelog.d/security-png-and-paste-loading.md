- Loading a workflow from a PNG, and pasting nodes, no longer unpickle the data unrestricted, and no
  longer run a node's element command other than the parameter-editing ones serialization writes.
  Either gap let a crafted image or clipboard payload run any command when loaded. Data from earlier
  versions is read by a reader that builds only saved value types and checks the same allowlist.
