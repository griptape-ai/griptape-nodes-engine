- The `Widget` trait takes a `synced_parameters` list naming other parameters on the node that a
  custom widget reads and writes directly, so a node no longer copies them into the widget's own
  value. The list is sent to the editor in the widget's UI options. See
  [Custom Widgets](https://docs.griptapenodes.com/en/stable/development/custom_nodes/custom_widgets/).
  [#4639](https://github.com/griptape-ai/griptape-nodes-engine/issues/4639)
