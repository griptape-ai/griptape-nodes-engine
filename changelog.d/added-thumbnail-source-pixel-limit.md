- `max_thumbnail_source_pixels` sets the stored-pixel limit checked before standard thumbnail
  decoding and defaults to `178956970`, matching the previous Pillow refusal threshold.
  Increasing it beyond Pillow's independent ceiling requires the OpenImageIO backend and can
  require substantial memory.
