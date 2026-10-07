- Image, video, audio, and 3D inputs set from a file path now store a macro path such as
  `{inputs}/cat.png` for files in the workspace, and the absolute path for files outside it,
  instead of copying the file into `staticfiles/` and storing a `localhost` URL. Workflows read
  those files from disk, so headless and published runs no longer need the desktop app running,
  and values saved as `localhost` URLs by earlier versions are read from disk too.
