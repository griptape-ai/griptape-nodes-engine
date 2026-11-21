- `Subflow Node Group` has an `On Start` port on its left side and a `Group Complete` port on its right,
  like `On Each` and `Loop Complete` on a `ForEach Group`. Wire `On Start` to the first node inside the
  group and the last node inside to `Group Complete`. Grouping a node that is already in a control chain
  wires them for you.
  [#6210](https://github.com/griptape-ai/griptape-nodes-engine/issues/6210)
