- Loading a workflow from a PNG, and pasting nodes, no longer unpickle the data unrestricted, no
  longer run a command in a place serialization never writes that type of command, and no longer
  import a saved control's module from outside Griptape Nodes and its libraries. Each gap let a
  crafted image or clipboard payload run any command or module when loaded. Data from earlier
  versions is read by a reader that builds only saved value types and applies the same checks.
