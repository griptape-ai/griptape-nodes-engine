- A loop body copy of a node now keeps every value the canvas node holds, including inputs fed by
  connections, so a node whose parameters depend on an input (such as a diffusers node whose
  `image_references` depends on `pipeline`) no longer loses them when the loop runs. A saved value the
  rebuilt node refuses on an input a connection feeds is logged and skipped instead of failing the whole
  rebuild, and rebuilt connections no longer push values into nodes while a loop body or pasted
  selection is still being assembled.
  [#6104](https://github.com/griptape-ai/griptape-nodes-engine/issues/6104)
