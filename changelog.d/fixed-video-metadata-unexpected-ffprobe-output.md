- Saving or previewing a video whose properties ffprobe reports in an unexpected form, such as a
  non-numeric width or a malformed frame rate, no longer fails with an error. Only the property
  that could not be read is left empty, and a warning names it and the file; the video's other
  properties (width, height, frame rate, duration, file size) are still read. If the file
  disappears before its size can be read, only the file size is left empty.
