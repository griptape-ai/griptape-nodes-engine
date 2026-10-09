- Installing a library's Python packages is no longer silent. When a library gets a new
  environment, or an update adds packages, the console shows at the default log level which
  packages it is installing, each download of about 100 MB or more (such as torch), when every package is
  ready to install, and how long the install took. Smaller downloads and the rest of the installer's
  output are logged at `DEBUG`, and routine startups, where nothing needs installing, print nothing
  new. `EngineInitializationProgress` carries the same progress in its new `detail` and
  `dependencies` fields, including every download, so the editor can show it while the library
  loads.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
