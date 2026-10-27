- The File Browser dialog's file preview no longer attempts a raw Pillow decode of formats with
  no registered artifact provider, such as EXR, which could hang the engine decoding large or
  multi-part image files. Unsupported formats now skip preview generation instead.
