- Directory parameters built with `ProjectDirectoryParameter` now use an absolute or `~/...` path
  as the destination instead of nesting it inside the outputs directory. A plain name still goes
  through the situation template and is versioned.
  [#6237](https://github.com/griptape-ai/griptape-nodes-engine/issues/6237)
