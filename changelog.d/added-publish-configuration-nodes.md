- Node libraries can give a publish target a Configuration node that remembers its publish settings.
  A library subclasses `ConfigurationNode` and names it with `configuration_node_type` in
  `PublishWorkflowRegisteredEventData`. The engine passes the node's values to
  `get_publish_options` and the publisher as `configuration`, adds the node after the first
  successful publish, writes back the `configuration` the publisher returns, and saves the
  workflow. A run never executes a Configuration node, and copying one is refused. Node library
  entries can set `hidden_from_library` to keep a node type out of the node library and search.
  See [Publishing a workflow](docs/guides/publishing.md#for-library-authors).
  [#5179](https://github.com/griptape-ai/griptape-nodes-engine/issues/5179)
