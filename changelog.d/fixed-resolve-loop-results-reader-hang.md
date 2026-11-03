- Resolving a node that reads a `ForEach` or `ForLoop` End's `results`, after editing an input to
  the loop body, now runs the loop again and finishes. Before, the loop never started, the body ran
  once outside the loop with a stale `current_item`, and the resolved node stayed running forever.
