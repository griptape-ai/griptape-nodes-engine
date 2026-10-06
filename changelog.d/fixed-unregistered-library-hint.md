- A library copied into `libraries_directory` without being registered no longer goes missing
  silently. Discovery logs the manifest's path and how to register it, and the
  `libraries_directory` description no longer claims libraries there load on startup: a library
  loads only from `libraries_to_register` (or Add Library) or a `libraries_to_download` entry.
  [#5749](https://github.com/griptape-ai/griptape-nodes-engine/issues/5749)
