- Installing a library's Python packages is no longer silent. When a library gets a new
  environment, the console shows at the default log level which packages it is installing. During
  any install, including one where a library update adds packages, it shows each download of about
  100 MB or more (such as torch) and when every package is ready to install. It also shows how long
  the install took, for a new environment or any install over 10 seconds. Smaller downloads and the
  rest of the installer's output are logged at `DEBUG`, and routine startups, where nothing needs
  installing, print nothing new. `EngineInitializationProgress` carries the progress in its new
  `detail` and `dependencies` fields, naming the download the install is waiting on, so the editor
  can show it while the library loads.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
