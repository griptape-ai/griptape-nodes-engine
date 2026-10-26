- Node metadata in a library JSON can declare a `role` (the same names presets use, such as `Edit` or
  `Generate`) and a `media` list (such as `["image", "video"]`), so editors can group nodes without
  guessing from tags. They come back wherever node metadata does, including
  `ListNodeTypesInLibraryRequest` with `include_metadata`. An unknown value is left out and
  reported as a library problem; the node still loads. Library schema version is now `0.16.0`.
