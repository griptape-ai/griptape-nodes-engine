- Direct `OpenImageIOThumbnailGenerator` calls convert scene-linear sources to sRGB with
  the built-in colour configuration. Unsupported colour spaces retain their pixel values and
  produce a warning.
  See [standard thumbnail source formats](docs/guides/assets.md#standard-thumbnail-source-formats).
  [#6472](https://github.com/griptape-ai/griptape-nodes-engine/issues/6472)
