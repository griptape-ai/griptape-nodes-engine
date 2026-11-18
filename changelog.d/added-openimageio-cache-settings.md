- Engine settings now include an **Image Processing** section under `openimageio` to configure the
  shared image tile cache and decoder limits (`openimageio.max_memory_mb`,
  `openimageio.max_open_files`, `openimageio.autotile_size`, `openimageio.deduplicate`,
  `openimageio.threads`, `openimageio.max_image_size_mb`, and `openimageio.failure_retries`),
  applied live without restarting the engine.
