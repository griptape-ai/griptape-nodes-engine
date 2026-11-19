- Reopening a workflow no longer brings back models a node has retired in its model dropdown. The
  dropdown is rebuilt from the node's current models and the selected model moves to its
  replacement. The node shows a "Model list updated" warning that names the retired models, and it
  disappears when the model next changes. Save the workflow to keep the update. Dropdowns a node
  fills with other models on purpose, such as an Agent's third-party provider list, are untouched.
  [#6052](https://github.com/griptape-ai/griptape-nodes-engine/issues/6052)
