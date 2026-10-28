- A node output filename typed without an extension, such as `bedroom_flipped_grid_bw`, now saves
  with the extension of the node's default filename. An absolute path or `file://` address is still
  saved exactly as typed. Before, the saved name ended in a bare dot (`bedroom_flipped_grid_bw_v001.`),
  which some network volumes reject, losing the result of a finished generation.
  [#6029](https://github.com/griptape-ai/griptape-nodes-engine/issues/6029)
