- `GetParameterValueRequest` handled inside the engine, as from node code or
  `RetainedMode.get_value`, returns the parameter's value itself instead of a plain-data copy, so
  an artifact comes back as the artifact.
