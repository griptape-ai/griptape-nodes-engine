- Saving a file from a node now writes an immutable provenance record alongside it — what
  produced the file, its content hash, parameter values, and a workflow snapshot — queryable
  through the new `GetProvenanceForArtifactRequest` family. Projects choose per-situation
  whether a failed record warns or stops the save.
  [#5626](https://github.com/griptape-ai/griptape-nodes-engine/issues/5626)
