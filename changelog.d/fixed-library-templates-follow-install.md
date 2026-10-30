- Installing a library mid-session puts its workflow templates in the workflow picker, and
  uninstalling one takes them out again, without restarting the engine. Updating or reloading a
  library picks up edits to its template files. Before, templates only appeared at engine start, an
  uninstalled library kept offering them, and an install -> uninstall -> reinstall cycle piled up
  stale entries.
  [#3448](https://github.com/griptape-ai/griptape-nodes-engine/issues/3448)
