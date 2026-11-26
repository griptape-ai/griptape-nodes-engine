- Direct `OpenImageIOThumbnailGenerator` calls convert scene-linear sources to sRGB with
  the built-in colour configuration. Unsupported colour spaces retain their pixel values and
  produce a warning.
  See [generator-level support](docs/guides/assets.md#generator-level-format-support).
  [#6472](https://github.com/griptape-ai/griptape-nodes-engine/issues/6472)
