- `ScanSequencesResultFailure`, `ListDirectoryResultFailure`, `ListDirectorySequencesResultFailure`,
  and `DeduceSequencesFromFileListResultFailure` read back from JSON with their `failure_reason`,
  instead of failing because it may come from either of two enums.
  [#5438](https://github.com/griptape-ai/griptape-nodes-engine/issues/5438)
