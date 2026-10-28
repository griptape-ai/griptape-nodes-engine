- The File Browser dialog's file preview no longer sends the entire file as a data URL for images
  over 10 MB that Pillow can't decode, which could hang the engine base64-encoding a large or
  multi-part sequence frame (e.g. EXR). Those files now get no preview instead. Images Pillow can
  decode still get a thumbnail regardless of file size.
