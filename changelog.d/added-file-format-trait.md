- A `FileFormat` trait marks a parameter's value as a file format of a given kind (`image`, `video`,
  `audio`, `threed`). It renders the kind as the `file_format` UI option, which the editor copies onto
  a Start Flow parameter and the workflow shape reports, so a host running the workflow can offer only
  the formats it can open.
  [#6193](https://github.com/griptape-ai/griptape-nodes-engine/issues/6193)
