- Nodes wired after a `Subflow Node Group` run. Grouping a node in the middle of a control chain now
  carries its `Flow In` / `Flow Out` wires on the group's own ports instead of adding extra ports, so the
  run continues past the group instead of ending there and still reporting success. The same applies to
  `ForEach Group` and `For Loop Group`, which no longer grow stray ports when a wired node is grouped.
  Groups saved before this keep the ports they were saved with; ungroup and regroup the node to pick up
  the new wiring.
  [#6210](https://github.com/griptape-ai/griptape-nodes-engine/issues/6210)
  [#5487](https://github.com/griptape-ai/griptape-nodes-engine/issues/5487)
