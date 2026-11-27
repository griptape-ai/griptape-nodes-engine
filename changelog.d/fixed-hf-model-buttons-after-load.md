- The model download button and the model list's refresh button on HuggingFace model parameters,
  such as the diffusers `PipelineBuilder` node's, now work right after a workflow or template
  opens instead of failing with "received message type 'on_click' but no handler was available".
  Picking a different model also shows or hides the download button again.
  [#6468](https://github.com/griptape-ai/griptape-nodes-engine/issues/6468)
