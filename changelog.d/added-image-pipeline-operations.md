- `GriptapeImagePipeline` can resize, crop, rotate, flip, pad, convert to gray or 8-bit, adjust
  brightness, contrast, and saturation, invert, blur, dilate, erode, find edges, composite, set
  alpha, and draw text, rectangles, and lines, keeping the image's metadata. `GriptapeImage` can be
  made from and read back as a NumPy array, or created in one color. Previews of grayscale images
  as WEBP or GIF no longer fail, GIF previews keep their colors, and JPEG, WEBP, and TIFF images
  saved with workflow metadata load their workflow in earlier releases again.
  [#6180](https://github.com/griptape-ai/griptape-nodes-engine/issues/6180)
