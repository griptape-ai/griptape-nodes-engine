- A node whose text holds a `{...}` token, such as `{inputs}/cat.png` on **Load Image**, no longer
  runs again, along with the nodes below it, on every play or Run when nothing changed. It now runs
  again only when its text would read differently.
  [#6249](https://github.com/griptape-ai/griptape-nodes-engine/issues/6249),
  [#6050](https://github.com/griptape-ai/griptape-nodes-engine/issues/6050)
