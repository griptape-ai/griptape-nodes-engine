- A `FileFormat` trait marks a parameter's value as a file format of a given kind (`image`, `video`,
  `audio`, `three_d`). The workflow shape reports the kind as `file_format` on the parameter, and on
  a Start Flow parameter when every parameter it feeds that declares one agrees, so a host can offer
  only the formats it can open.
