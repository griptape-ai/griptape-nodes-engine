- Workflows with a list parameter of a custom item type, such as a `Display List` fed a
  `list[Thing]`, now reopen with each item's value intact instead of rejecting it as the wrong type,
  and the list's output type no longer gains an extra `list[...]` each time the workflow is saved.
  [#6152](https://github.com/griptape-ai/griptape-nodes-engine/issues/6152)
