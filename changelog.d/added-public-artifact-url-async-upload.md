- `PublicArtifactUrlParameter` has `aget_public_url_for_parameter()` and
  `adelete_uploaded_artifact()`. Nodes using them can upload multiple reference images concurrently
  without blocking other nodes or delaying "Stop". Uploads interrupted by "Stop" finish and are
  deleted in the background.
  [#5729](https://github.com/griptape-ai/griptape-nodes-engine/issues/5729)
