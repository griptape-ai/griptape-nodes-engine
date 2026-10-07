- Image, video, audio, and 3D inputs set from a file path now keep that file's absolute path
  instead of copying it into `staticfiles/` and storing a `localhost` URL. Workflows read those
  files from disk, so headless and published runs no longer need the desktop app running, and
  values saved as `localhost` URLs by earlier versions are read from disk too.
