- When a library installs its Python packages into a new environment, the engine now prints which
  packages it is installing and how long the install took, and says it can take several minutes.
  `EngineInitializationProgress` carries the same information in its new `detail` and
  `dependencies` fields, and `detail` updates as large packages download, for example
  "Downloading torch (2.0GiB) and 3 more for Diffusers...", so the editor can show it while the
  library loads.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
