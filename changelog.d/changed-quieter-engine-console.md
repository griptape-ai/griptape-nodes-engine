- The engine console no longer prints routine messages, such as library dependency installs,
  `Resolving <node>`, `Flow is complete.`, workflow saves, loop iterations, workflow sync, worker
  and session bookkeeping, and agent prompts and tool calls. A failed node is reported once, by the
  run that hit it, instead of at every layer with a full traceback. Set `log_level` to `DEBUG` to
  see the rest.
