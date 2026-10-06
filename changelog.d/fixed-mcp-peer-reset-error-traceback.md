- On Windows, a client resetting a connection to the engine's MCP server while it was already closing
  no longer logs an ERROR traceback that looks like a crash. It is logged at debug, and other errors
  on that server still log as before.
  [#5745](https://github.com/griptape-ai/griptape-nodes-engine/issues/5745)
