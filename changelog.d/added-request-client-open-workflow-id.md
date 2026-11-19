- `RequestClient.request` and `RequestClient.request_batch` now take an `open_workflow_id`
  argument, so a Python script can name the open workflow its requests act on while several
  workflows are open.
  [#6381](https://github.com/griptape-ai/griptape-nodes-engine/issues/6381)
