- `ReloadAllLibrariesRequest` now keeps every open workflow open, rebuilding its nodes from the
  reloaded libraries, instead of closing them all. Unsaved work survives a library reload, and
  an editor keeps its tabs. During the reload, opening a workflow or sending a request that names
  an open one fails, so send it again once the reload finishes.
