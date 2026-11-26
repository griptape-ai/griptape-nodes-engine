- Image metadata drivers can override `BaseImageMetadataDriver.inject_metadata_into_bytes` to
  edit encoded content without decoding pixels. Existing drivers use the default Pillow
  fallback through `inject_metadata`.
