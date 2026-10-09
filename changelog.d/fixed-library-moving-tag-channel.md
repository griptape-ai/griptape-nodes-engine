- A library on a moving tag such as `stable` keeps following that tag when updated, instead of
  switching to `nightly` whenever both tags point at the same commit. The library checkout records
  the tag it follows, and Library Management reports it as the current ref.
  [#5744](https://github.com/griptape-ai/griptape-nodes-engine/issues/5744)
