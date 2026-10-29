- `LoadMetadataForAllLibrariesRequest` now takes `include_git_info`. Pass `false` to skip the git
  lookups behind `git_remote` and `git_ref`, which take several git processes per library, when
  only the library schemas are needed.
  [#5943](https://github.com/griptape-ai/griptape-nodes-engine/issues/5943)
