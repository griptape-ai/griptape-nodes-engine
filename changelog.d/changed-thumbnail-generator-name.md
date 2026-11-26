- **Breaking:** `PILThumbnailParameters` is now `ThumbnailParameters`, shared by both thumbnail
  backends. The built-in `Image` provider selects the standard thumbnail backend at startup.
  For direct calls, use `OpenImageIOThumbnailGenerator` or `PILThumbnailGenerator`.
  See [MIGRATION.md](MIGRATION.md#thumbnail-generator-imports).
