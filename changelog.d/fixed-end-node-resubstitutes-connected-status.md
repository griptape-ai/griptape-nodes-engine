- A value that reaches a node over a connection is no longer run through variable substitution
  again when the node copies it onto its own output. A save node's `Saved to: {outputs}/a.mp4`
  status wired into an End node now stays a macro on the End node too, so the saved workflow
  holds no absolute path. [#5897](https://github.com/griptape-ai/griptape-nodes-engine/issues/5897)
