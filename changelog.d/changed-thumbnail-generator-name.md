- **Breaking:** `PILThumbnailParameters` is now `ThumbnailParameters`, shared by both thumbnail
  backends. `ThumbnailGenerator` selects OpenImageIO or Pillow automatically, and
  `PILThumbnailGenerator` remains available for Pillow.
  See [MIGRATION.md](MIGRATION.md#thumbnail-generator-imports).
