- Sequence scans (`ScanSequencesRequest`) now find frames whose number follows a hyphen, such as
  `PF0044-0001.png` for `PF0044-####.png`, and directory listings group them into a sequence. The
  hyphen was read as a minus sign, so the template failed to parse or the scan came back empty.
  [#5999](https://github.com/griptape-ai/griptape-nodes-engine/issues/5999)
