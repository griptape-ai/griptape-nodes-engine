- When YAML Input rejects text, the error now names the line and column, quotes the line, and, for
  a value that contains a colon followed by a space, says to wrap the value in quotes or use `|` to
  put the text on the next line.
  [#6397](https://github.com/griptape-ai/griptape-nodes-engine/issues/6397)
