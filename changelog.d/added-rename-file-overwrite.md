- `RenameFileRequest` takes `overwrite`, which replaces an existing destination instead of failing.
  A file onto a file is replaced in one step. When either side is a folder, the existing
  destination is only deleted once the rename succeeds, and is put back if it fails.
  [#6457](https://github.com/griptape-ai/griptape-nodes-engine/issues/6457)
