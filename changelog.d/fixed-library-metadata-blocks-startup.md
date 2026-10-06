- Loading library metadata no longer stalls the engine while it reads each library's git details.
  On Windows, the editor's repeated metadata requests during startup could hold the engine on
  "Starting your engine" for several minutes.
  [#5767](https://github.com/griptape-ai/griptape-nodes-engine/issues/5767)
