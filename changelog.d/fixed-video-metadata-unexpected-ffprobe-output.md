- Saving or previewing a video whose properties ffprobe reports in an unexpected form, such as a
  non-numeric width or a malformed frame rate, no longer fails with an error. The video's
  properties (width, height, frame rate, duration) are left empty instead, and a warning names
  the file. The same applies when the file disappears before its size can be read.
