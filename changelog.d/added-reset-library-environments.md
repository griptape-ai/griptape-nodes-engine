- `ResetLibraryRequest` deletes a library's Python environments (`.venv` and `.venv-exec`) and
  builds them again, so a library whose packages are broken can be recovered without deleting
  folders by hand. When the engine has already loaded packages from the library, the reset finishes
  the next time the engine starts and the result reports `restart_required`.
  `LoadMetadataForAllLibrariesRequest` reports `reset_requires_restart` so a client can say so
  before the reset is confirmed.
  [#5102](https://github.com/griptape-ai/griptape-nodes-engine/issues/5102)
