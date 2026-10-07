- Outputs of a node in a library running in its own process reach downstream nodes as the same
  types, instead of as plain dicts. Artifact classes a library defines itself, such as
  `VideoUrlArtifact`, arrive as that class instead of griptape's class of the same name. A value
  whose class belongs to a library the receiving process does not load still arrives as a dict, and
  reaches the next process that does load it unchanged.
  [#4475](https://github.com/griptape-ai/griptape-nodes-engine/issues/4475)
