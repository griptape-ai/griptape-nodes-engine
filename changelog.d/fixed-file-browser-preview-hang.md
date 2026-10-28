- The File Browser dialog's file preview no longer sends the entire file as a data URL for images
  over 10 MB, which could hang the engine base64-encoding a large or multi-part sequence frame
  (e.g. EXR). Files over that size now get no preview instead.
