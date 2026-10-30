- Model downloads can be paused with `PauseModelDownloadRequest` and resumed with
  `ResumeModelDownloadRequest`, and stay paused across engine restarts. Resuming skips files that
  already finished. Nodes won't run on a paused or failed download.
  [#4563](https://github.com/griptape-ai/griptape-nodes-engine/issues/4563)
