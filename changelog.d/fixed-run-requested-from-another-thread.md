- A run requested synchronously from another thread while a run is in progress now waits its turn on
  the engine loop, so two runs can no longer start at once.
  [#6039](https://github.com/griptape-ai/griptape-nodes-engine/issues/6039)
