- Video features now use `ffmpeg` and `ffprobe` from your `PATH` when both are installed, instead
  of downloading their own copy, so they work on networks that block the download. Binaries
  supplied through the `ffmpeg_directory` setting still take priority over `PATH`.
