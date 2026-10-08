- Saving an image no longer floods the editor with engine bookkeeping: element events for hidden
  copies of every node, and results of the engine's own path and project lookups. A 72-node image
  workflow sent 66 MB over the websocket per run, and now sends 6 MB.
  [#2823](https://github.com/griptape-ai/griptape-vsl-gui/issues/2823)
