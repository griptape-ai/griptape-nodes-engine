- Image previews, thumbnails, and file metadata now decode and encode through OpenImageIO instead
  of Pillow, adding preview and header support for OpenEXR (`.exr`), Radiance HDR (`.hdr`), DPX
  (`.dpx`), Cineon (`.cin`), Photoshop (`.psd`), JPEG 2000 (`.jp2`), Netpbm and PFM (`.pfm`,
  `.ppm`, `.pgm`, `.pbm`), SGI (`.sgi`), and DirectDraw Surface (`.dds`) files, along with 16-bit
  integer and 16/32-bit float images. Node libraries that import `PIL` must declare `pillow` in
  their own `pip_dependencies`.
