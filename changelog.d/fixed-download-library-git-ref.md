- `DownloadLibraryRequest` now honors a `url@ref` suffix on `git_url`, checking out that branch,
  tag, or commit instead of failing to clone. An explicit `branch_tag_commit` still takes precedence.
