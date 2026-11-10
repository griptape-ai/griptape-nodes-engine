- A loop body copy of a node now keeps every value the canvas node holds, including inputs fed by
  connections, so a node whose parameters depend on an input (such as a diffusers node whose
  `image_references` depends on `pipeline`) no longer loses them when the loop runs. The copy's
  connections are made the way a saved workflow makes them, so they push nothing into nodes while the
  body is still being assembled, and a saved value the copy refuses on a connected input is logged and
  skipped instead of stopping the loop.
  [#6104](https://github.com/griptape-ai/griptape-nodes-engine/issues/6104)
