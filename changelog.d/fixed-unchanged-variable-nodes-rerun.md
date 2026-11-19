- A node whose text holds a `{...}` token, such as a project path like `{inputs}/cat.png` on
  **Load Image**, no longer runs again on every play or Run when nothing changed. Its re-run also
  made a generation node below it run again on the next play. Such a node now runs again, along
  with everything downstream of it, when its text would read differently: a variable changed, the
  project changed, or a project variable was changed from another open workflow.
  [#6249](https://github.com/griptape-ai/griptape-nodes-engine/issues/6249),
  [#6050](https://github.com/griptape-ai/griptape-nodes-engine/issues/6050)
