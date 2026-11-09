- The image preview generators `PILThumbnailGenerator` and `PILRoundedPreviewGenerator` are now
  `ThumbnailGenerator` and `RoundedPreviewGenerator`, in the `thumbnail_generator` and
  `rounded_preview_generator` modules. Libraries that register or import them by the old names
  need updating; their names in the editor and in saved settings are unchanged.
  [#6180](https://github.com/griptape-ai/griptape-nodes-engine/issues/6180)
