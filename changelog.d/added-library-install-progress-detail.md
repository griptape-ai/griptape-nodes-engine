- Installing a library's Python packages is no longer silent. At the default log level the console
  shows which packages a new environment installs, each download of about 100 MB or more (such as
  torch), when the packages are ready to install, and how long a new or slow install took. Smaller
  downloads and other installer output stay at `DEBUG`, and routine startups print nothing new.
  `EngineInitializationProgress` carries the same progress in its new `detail` and `dependencies`
  fields, so the editor can show it while the library loads.
  [#5207](https://github.com/griptape-ai/griptape-nodes-engine/issues/5207)
