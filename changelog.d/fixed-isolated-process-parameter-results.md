- A node that reports a result with `set_parameter_value` now shows that result when the node runs
  in a library's isolated process, instead of leaving the output empty. A parameter that has an
  output, set while the node is running, now also records the value as a result, and results are what
  travel back from an isolated process. This covers a parameter that is also kept on display, and one
  that declares no modes at all, which is most of the parameters a library writes. Nothing the
  parameter held before is given up: the value is still the parameter's own, so a node that sets one
  mid-run and reads it on the next run, as a randomized seed does, reads what it set. A parameter
  with no output has nowhere to publish, so a value set on it during a run stays in the process that
  set it.
  [#5663](https://github.com/griptape-ai/griptape-nodes-engine/issues/5663)
