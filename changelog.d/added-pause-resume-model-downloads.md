- Model downloads can be paused and resumed. `PauseModelDownloadRequest` stops a download, its
  status reads `paused`, and it stays paused across engine restarts. `ResumeModelDownloadRequest`
  continues it with the revision and folder it was started with, skipping the files that had
  finished; the file that was in progress downloads again.
  [#4563](https://github.com/griptape-ai/griptape-nodes-engine/issues/4563)
