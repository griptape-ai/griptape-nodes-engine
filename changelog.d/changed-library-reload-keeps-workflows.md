- `ReloadAllLibrariesRequest` now keeps every open workflow open, rebuilding its nodes from the
  reloaded libraries, instead of closing them all. Unsaved work survives a library reload, and
  an editor keeps its tabs.
