- **Breaking:** A request that also works without a workflow, such as resolving a path, making a
  preview or download URL, a budget check, a usage report or a model invocation, now acts on no
  workflow when it names none, instead of on the only open workflow. With several workflows open
  these requests no longer fail. With one open, `{workflow_dir}` no longer resolves for them, and a
  model invocation is refused. Node code that sends one from a thread it starts should send it with
  `self.workflow.handle_request(...)`. See
  [Saving from a Thread or Thread Pool](docs/development/custom_nodes/project_system.md#saving-from-a-thread-or-thread-pool).
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
