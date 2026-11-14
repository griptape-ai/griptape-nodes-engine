- Nodes wired after a `Subflow Node Group` run. Grouping a node in the middle of a control chain keeps
  its control wires on the group's own ports instead of adding extra ports: outside the group they meet
  `Flow In` / `Flow Out`, inside they run from `On Start` and into `Group Complete`, or `On Each` and
  `Loop Complete` on a `ForEach Group` or `For Loop Group`. The run continues past the group instead of
  ending there and still reporting success. Ungrouping the node puts the chain back when only one wire
  could have been its own. Grouping a node in the editor no longer deletes its data wires.
  Groups saved before this still stop at the group. Ungroup and regroup the node, or wire the group's
  `Flow Out` (`On Complete` on a loop) to the next node by hand; the old extra ports stay on the group.
  [#6210](https://github.com/griptape-ai/griptape-nodes-engine/issues/6210)
  [#5487](https://github.com/griptape-ai/griptape-nodes-engine/issues/5487)
