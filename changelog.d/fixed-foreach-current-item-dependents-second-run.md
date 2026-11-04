- A data node fed by a ForEach or ForLoop Start's `current_item` or `index` now re-evaluates on every
  pass of every run, not only the first. On later runs it kept its value from the last pass of the
  previous run and fed it to every pass.
  [#5751](https://github.com/griptape-ai/griptape-nodes-engine/issues/5751)
