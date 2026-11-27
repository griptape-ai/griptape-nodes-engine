- `UninstallLibraryRequest` removes an installed node library: it unregisters the library,
  unloads it, and sends its folder to the recycle bin where the platform supports one. Only
  libraries the engine installed are removed this way, so a library you pointed the engine at
  yourself keeps its folder. A folder still in use is removed at the next engine start.
  `PreviewUninstallLibraryRequest` reports what would be removed, and says when a library
  cannot be uninstalled, so the editor can disable the control before anything is clicked.
