- `InspectLibraryRepoRequest` now honors a `url@ref` suffix on `git_url` (for example
  `user/repo@stable`), as `DownloadLibraryRequest` already does, instead of failing to reach the
  repository. `ref` is now optional and overrides the suffix when set.
