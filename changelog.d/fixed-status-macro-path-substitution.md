- A node's status no longer has project variables substituted into it during a run, so a status
  such as `Saved to: {outputs}/a.mp4` is saved with the workflow as written instead of as the
  absolute path. [#5897](https://github.com/griptape-ai/griptape-nodes-engine/issues/5897)
