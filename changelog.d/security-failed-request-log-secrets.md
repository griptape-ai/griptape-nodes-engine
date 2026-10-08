- A request that fails outside its handler is now logged by type and request ID only. The engine
  used to log the whole request, so a failed `SetSecretValueRequest` wrote the secret value to the
  engine log in plain text.
  [#5739](https://github.com/griptape-ai/griptape-nodes-engine/issues/5739)
