- Reloading libraries, switching projects, or updating a library no longer marks a library that
  registers an artifact provider as flawed with a "duplicate friendly name" error. Unloading a
  library now removes its artifact providers, so the next load registers its current provider code.
  [#6307](https://github.com/griptape-ai/griptape-nodes-engine/issues/6307)
