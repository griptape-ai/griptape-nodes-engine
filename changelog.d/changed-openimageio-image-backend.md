- **Breaking:** Image previews, thumbnails, and file metadata now use OpenImageIO. Node libraries
  that import `PIL` must declare `pillow` in their own dependencies. See
  [MIGRATION.md](MIGRATION.md#image-processing-uses-openimageio) for dependency and preview
  generator import changes. Previews now support OpenEXR, Radiance HDR, DPX, Cineon, Photoshop,
  JPEG 2000, Netpbm, PFM, SGI and DirectDraw Surface files, including high-bit-depth images.
  [#6144](https://github.com/griptape-ai/griptape-nodes-engine/issues/6144)
