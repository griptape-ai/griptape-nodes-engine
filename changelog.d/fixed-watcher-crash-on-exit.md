- A process running the engine no longer risks crashing with a segmentation fault as it exits after
  refreshing a preview or syncing workflows. The threads that watch those files now stop before
  Python shuts down.
