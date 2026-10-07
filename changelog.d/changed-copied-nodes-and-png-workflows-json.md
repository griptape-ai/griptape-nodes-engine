- Copied nodes, and the workflow embedded in an exported PNG, are stored as JSON instead of pickle,
  and the embedded workflow is compressed, so exported PNG files are smaller. Nodes copied and PNG
  files exported by earlier versions still paste and load. A PNG exported by this version doesn't
  load its workflow in earlier ones.
