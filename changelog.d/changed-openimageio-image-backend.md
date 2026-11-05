- Images are read and written with OpenImageIO. 16-bit and float images keep their bit depth,
  EXR and TGA files open, and palette images open in full color. A float image without a color
  profile saved as PNG, JPEG, or WEBP is converted for display, so it doesn't come out dark. Resizing and rotating keep
  a photo's EXIF and color profile, and saving to a format that can't hold some of an image's
  metadata logs a warning naming what was left out.
  [#6180](https://github.com/griptape-ai/griptape-nodes-engine/issues/6180)
